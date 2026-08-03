"""Speech to text, on this machine, in a process the daemon can lose.

The model is Parakeet through `parakeet-mlx`, the same arrangement the dictation toolkit
uses: about a gigabyte of weights, loaded once, kept resident, and fed 16 kHz mono audio.
What is worth explaining is not the model but where it runs.

**Why a subprocess, and not a thread.** Three reasons, and each of them alone would be
enough. MLX loading a model spawns native threads and wires GPU memory; the daemon is the
process that must stay single-threaded enough to be predictable and small enough to restart
cheaply, and it deliberately imports none of the heavy stack — `frank.computer` and the
runtime are both kept out of it for the same family of reasons. Inference can hang: a wedged
GPU stream is not recoverable in-process, and the only honest fix is to replace the process
holding it. And a crash inside a model is a crash: in a thread it takes the daemon and every
session's control plane with it, while here it takes a worker nobody was using between
recordings.

**Why it is lazy.** Nothing is imported and no weights are read until the first transcription
is asked for. A person who never turns dictation on never pays for it; a person who turns it on
pays the load once, and then the worker stays warm for as long as they keep dictating.

**What a failure looks like.** Every failure — the package missing, the download failing, the
worker dying, inference hanging — comes back as :class:`DictationUnavailable` carrying a
sentence a person can act on. The composer shows that sentence. Nothing is retried silently
except the one case where retrying is right: a worker that died or hung is replaced once and
the same audio is submitted again, because the audio is somebody's voice and asking them to
say it a second time is the worst answer available.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import uuid
from typing import Any, Optional

from frank.base.errors import summary

logger = logging.getLogger(__name__)

# Parakeet expects 16 kHz mono, and whatever recorded has already resampled to it — a browser
# through its `AudioContext`, the phone app through the platform's audio framework. So the
# samples arriving here need no conversion, which is the whole reason the wire carries raw
# float32 rather than an encoded file the daemon would need a codec to open.
#
# Deliberately a constant while every wait below is a setting: this is what the model takes, so
# it is a fact rather than a preference, and a configuration key for it would only be a way to
# make transcription quietly wrong.
SAMPLE_RATE = 16000

# How long to wait, and how hard to try, come from `dictation.timing` in the configuration —
# they are what a slow machine or a slow connection needs to move. `frank configure --all`
# lists them with their defaults and their units.


class DictationUnavailable(RuntimeError):
    """Dictation could not be served, with the reason a person should be shown."""


# Why a worker could not start, as a value rather than as prose to be matched against. The
# parent has to tell these apart to say anything useful — "install the package" and "the
# download failed" are different instructions — and pattern-matching a traceback for the
# difference is a test that breaks the day an exception is reworded.
STARTUP_MISSING_PACKAGE = "missing_package"
STARTUP_LOAD_FAILED = "load_failed"




def _worker_main(request_queue, response_queue, model_identifier: str, parent_process_identifier: int) -> None:
    """Load the model once, then answer transcription requests until told to stop."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Spawned, so nothing about the daemon's logging is inherited. Configured here, against the
    # same file the daemon writes, so a failure in this process is readable beside the request
    # that caused it instead of going to a stderr nobody keeps.
    from frank.base.paths import log_file_path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file_path("frankd"))],
    )

    def exit_with_parent() -> None:
        """Leave when the daemon does. A worker holding a gigabyte of wired GPU memory must not
        outlive the process that was using it — and being orphaned onto init is precisely the
        state in which nothing would ever ask it to stop."""
        while os.getppid() == parent_process_identifier:
            time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=exit_with_parent, name="dictation-parent-watchdog", daemon=True).start()

    try:
        import mlx.core
        from parakeet_mlx import from_pretrained
        from parakeet_mlx.audio import get_logmel

        mlx.core.set_default_device(mlx.core.gpu)
        mlx.core.set_default_stream(mlx.core.new_stream(mlx.core.gpu))
        model = from_pretrained(model_identifier)
        # Materialised eagerly so the first transcription is inference rather than a lazy
        # load, which would otherwise land inside somebody's first recording.
        try:
            mlx.core.eval(model.parameters())
        except Exception:  # noqa: BLE001 — an eager materialise that fails only costs latency
            logger.warning("could not materialise the dictation model eagerly", exc_info=True)
        response_queue.put(("ready", "", ""))
    except ModuleNotFoundError as error:
        logger.exception("dictation worker could not import its dependencies", extra={"model": model_identifier})
        response_queue.put(("startup_failed", STARTUP_MISSING_PACKAGE, summary(error)))
        return
    except BaseException as error:  # noqa: BLE001 — every startup failure is reported, never raised into the void
        logger.exception("dictation worker could not load the model", extra={"model": model_identifier})
        response_queue.put(("startup_failed", STARTUP_LOAD_FAILED, summary(error)))
        return

    def transcribe(samples) -> str:
        audio = mlx.core.array(samples)
        results = model.generate(get_logmel(audio, model.preprocessor_config))
        return results[0].text.strip() if results else ""

    while True:
        request = request_queue.get()
        if request[0] == "stop":
            return
        _, request_identifier, samples = request
        try:
            try:
                text = transcribe(samples)
            except Exception as error:  # noqa: BLE001 — a fouled stream is recoverable in place, once
                logger.warning(
                    "dictation stream fouled, resetting and retrying once: %s", summary(error),
                )
                mlx.core.set_default_stream(mlx.core.new_stream(mlx.core.gpu))
                text = transcribe(samples)
            response_queue.put(("text", request_identifier, text))
        except BaseException as error:  # noqa: BLE001 — the caller is owed an answer, including a bad one
            logger.exception("dictation transcription failed", extra={"request": request_identifier})
            response_queue.put(("failed", request_identifier, summary(error)))
        finally:
            try:
                mlx.core.clear_cache()
            except Exception:  # noqa: BLE001 — a cache that will not clear is not a failed request
                logger.debug("could not clear the MLX cache", exc_info=True)


# What the model is doing, as the interface needs to know it. Loading is a *state*, not a wait:
# it takes as long as it takes — a first-run download over an unknown connection — and the honest
# way to present that is a control that says "loading" until it says "ready", never a deadline
# after which a perfectly healthy download is declared a failure.
STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_FAILED = "failed"


class SpeechTranscriber:
    """Owns the worker process, and replaces it when it stops answering.

    Loading happens off to one side: :meth:`ensure_started` returns at once and the model comes
    up on a thread, so the interface can show it arriving instead of blocking on it. Transcribing
    is one at a time by construction — a single lock, because there is one model in one process
    and a person dictates one thing at a time. Concurrency there would buy nothing and would make
    the "is this worker wedged" question unanswerable."""

    def __init__(self, model_identifier: str, timing) -> None:
        self._model_identifier = model_identifier
        # The `dictation.timing` section, held rather than unpacked: a worker replaced halfway
        # through a session should use the limits in force now, not the ones that happened to be
        # loaded when this object was built.
        self._timing = timing
        self._context = multiprocessing.get_context("spawn")
        self._process: Optional[Any] = None
        self._requests: Optional[Any] = None
        self._responses: Optional[Any] = None
        # Two locks, and they are separate on purpose: loading holds `_lock` for as long as a
        # download takes, and a status read that had to queue behind it would defeat the whole
        # point of reporting progress.
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = STATE_IDLE
        self._failure = ""
        self._settled = threading.Event()
        self._loader: Optional[threading.Thread] = None
        self._closed = False

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    @property
    def state(self) -> str:
        """`idle`, `loading`, `ready`, or `failed` — what the microphone button should show."""
        with self._state_lock:
            # A worker that died between transcriptions leaves the state stale; the process is
            # the fact, so it wins.
            if self._state == STATE_READY and not (self._process is not None and self._process.is_alive()):
                self._state = STATE_IDLE
            return self._state

    @property
    def failure(self) -> str:
        """Why loading failed, in a sentence, or empty when it has not."""
        with self._state_lock:
            return self._failure

    def _set_state(self, state: str, failure: str = "") -> None:
        with self._state_lock:
            self._state = state
            self._failure = failure

    def ensure_started(self) -> None:
        """Begin loading the model if it is not already loaded or loading. Returns immediately.

        Called when a composer with dictation switched on comes into view, so the weights are
        being fetched while somebody reads their conversation rather than while they wait with a
        finger on a microphone button."""
        with self._state_lock:
            if self._closed or self._state in (STATE_LOADING, STATE_READY):
                return
            self._state = STATE_LOADING
            self._failure = ""
            self._settled.clear()
        self._loader = threading.Thread(target=self._load, name="frank-dictation-loader", daemon=True)
        self._loader.start()

    def _load(self) -> None:
        try:
            with self._lock:
                if self._closed:
                    return
                if not (self._process is not None and self._process.is_alive()):
                    self._start()
            self._set_state(STATE_READY)
        except DictationUnavailable as error:
            self._set_state(STATE_FAILED, str(error))
        except Exception as error:  # noqa: BLE001 — a loader thread must never die silently
            logger.exception("dictation model could not be prepared")
            self._set_state(STATE_FAILED, summary(error))
        finally:
            self._settled.set()

    def _start(self) -> None:
        """Bring a worker up and wait for it to report that the model is loaded.

        Waits without a deadline, and that is the design: the first load includes fetching about
        a gigabyte over a connection nobody here can predict, so any number chosen would
        eventually call a working download a failure. What ends this wait is an answer — ready,
        or a worker that died — and until one arrives the state stays `loading`, which is exactly
        what the interface shows."""
        self._stop_process()
        self._requests = self._context.Queue()
        self._responses = self._context.Queue()
        self._process = self._context.Process(
            target=_worker_main,
            args=(self._requests, self._responses, self._model_identifier, os.getpid()),
            name="frank-dictation",
            daemon=True,
        )
        self._process.start()
        while True:
            # The queue is read before liveness is judged, and that order is the whole of it: a
            # worker that fails to start writes *why* and then exits, so testing the process
            # first wins the race often enough to turn every real reason — a missing package, a
            # failed download — into "it could not be started", which is the one message that
            # helps nobody. A dead process is only fatal once its queue has nothing left.
            try:
                kind, reason, summary = self._responses.get(timeout=0.2)
            except queue.Empty:
                if self._closed:
                    self._stop_process()
                    raise DictationUnavailable("Dictation is shutting down.")
                if not self._process.is_alive():
                    self._stop_process()
                    raise DictationUnavailable(
                        "The dictation model could not be started. The daemon log says why."
                    )
                continue
            if kind == "ready":
                logger.info("dictation model loaded", extra={"model": self._model_identifier})
                return
            self._stop_process()
            # The worker already logged the traceback where its frames mean something. What
            # crosses the queue is the reason as a value, so the sentence a person is shown can
            # be chosen rather than guessed at from the shape of a stack trace.
            logger.error("dictation worker failed to start: %s (%s)", reason, summary)
            if reason == STARTUP_MISSING_PACKAGE:
                raise DictationUnavailable(
                    "Dictation needs the `parakeet-mlx` package, which is not installed in this "
                    "environment. Run `uv sync` in the Frank repository and restart the daemon."
                )
            raise DictationUnavailable(
                "The dictation model could not be loaded — the download may have failed. "
                "Check the connection and try again."
            )

    def _stop_process(self) -> None:
        process, self._process = self._process, None
        requests, self._requests = self._requests, None
        self._responses = None
        if process is None:
            return
        try:
            if requests is not None and process.is_alive():
                requests.put(("stop",))
            process.join(self._timing.worker_shutdown_seconds)
            if process.is_alive():
                process.terminate()
                process.join(self._timing.worker_shutdown_seconds)
        except Exception:  # noqa: BLE001 — a worker that will not go quietly is killed above
            logger.debug("could not stop the dictation worker cleanly", exc_info=True)

    def transcribe(self, samples) -> str:
        """Transcribe one recording. Blocking; call it off the event loop.

        `samples` is mono float32 at :data:`SAMPLE_RATE`. If the model is still loading this
        waits for it rather than refusing — somebody who spoke anyway should get their words,
        not an error telling them to try again."""
        if self._closed:
            raise DictationUnavailable("Dictation is shutting down.")
        self.ensure_started()
        self._settled.wait()
        if self.state == STATE_FAILED:
            raise DictationUnavailable(self.failure or "Dictation is unavailable.")

        duration_seconds = len(samples) / float(SAMPLE_RATE)
        timeout = max(
            self._timing.minimum_transcription_timeout_seconds,
            duration_seconds * self._timing.transcription_timeout_realtime_multiplier,
        )
        with self._lock:
            if self._closed:
                raise DictationUnavailable("Dictation is shutting down.")
            last_failure = ""
            attempts = max(1, self._timing.maximum_attempts)
            for attempt in range(attempts):
                if not (self._process is not None and self._process.is_alive()):
                    self._start()
                    self._set_state(STATE_READY)
                assert self._requests is not None and self._responses is not None
                request_identifier = uuid.uuid4().hex
                self._requests.put(("transcribe", request_identifier, samples))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if not self._process.is_alive():  # type: ignore[union-attr]
                        last_failure = "the dictation worker stopped"
                        break
                    try:
                        response = self._responses.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    kind, identifier, detail = response
                    if identifier != request_identifier:
                        continue  # a straggler from a request that already timed out
                    if kind == "text":
                        return detail
                    # One line, and no traceback: the worker logged that where its frames are
                    # still attached to the code that raised them. This says which request, and
                    # what it was.
                    logger.error("dictation transcription failed: %s", detail)
                    last_failure = "the transcription failed"
                    break
                else:
                    last_failure = "the transcription timed out"
                # Whatever went wrong, this worker is not to be trusted with the retry. The
                # audio is, so it is submitted again to a fresh one.
                logger.warning(
                    "dictation attempt %d of %d failed (%s), replacing the worker",
                    attempt + 1, attempts, last_failure,
                )
                self._stop_process()
                self._set_state(STATE_IDLE)
            raise DictationUnavailable(f"Could not transcribe the recording — {last_failure}.")

    def close(self) -> None:
        # Flagged before the lock is taken: a load in flight holds it, and the point of closing
        # is to end that rather than to queue behind it.
        self._closed = True
        self._settled.set()
        with self._lock:
            self._stop_process()
        self._set_state(STATE_IDLE)


__all__ = [
    "DictationUnavailable",
    "SpeechTranscriber",
    "SAMPLE_RATE",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_LOADING",
    "STATE_READY",
]
