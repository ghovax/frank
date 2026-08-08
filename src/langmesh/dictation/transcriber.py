"""Speech to text on this machine, in a worker process the daemon can lose and replace."""

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

from langmesh.base.errors import summary

logger = logging.getLogger(__name__)

# Parakeet expects 16 kHz mono, which whatever recorded has already resampled to.
SAMPLE_RATE = 16000

# How long to wait and how hard to try come from the configuration, since a slow machine needs different numbers.


class DictationUnavailable(RuntimeError):
    """Dictation could not be served, with the reason a person should be shown."""


# Why a worker could not start, as a value rather than prose, because the remedies differ.
STARTUP_MISSING_PACKAGE = "missing_package"
STARTUP_LOAD_FAILED = "load_failed"


def _worker_main(
    request_queue, response_queue, model_identifier: str, parent_process_identifier: int
) -> None:
    """Load the model once, then answer transcription requests until told to stop."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Spawned, so nothing about the daemon's logging is inherited; configured here against the same file.
    from langmesh.base.paths import log_file_path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file_path("langmeshd")),
        ],
    )

    def exit_with_parent() -> None:
        """Leave when the daemon does, so a worker holding a gigabyte of wired memory is never orphaned."""
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
        # Materialised eagerly, so the first transcription is inference rather than a lazy load inside somebody's recording.
        try:
            mlx.core.eval(model.parameters())
        except Exception:  # noqa: BLE001 — an eager materialise that fails only costs latency
            logger.warning("could not materialise the dictation model eagerly", exc_info=True)
        response_queue.put(("ready", "", ""))
    except ModuleNotFoundError as error:
        logger.exception(
            "dictation worker could not import its dependencies", extra={"model": model_identifier}
        )
        response_queue.put(("startup_failed", STARTUP_MISSING_PACKAGE, summary(error)))
        return
    except BaseException as error:  # noqa: BLE001 — every startup failure is reported, never raised into the void
        logger.exception(
            "dictation worker could not load the model", extra={"model": model_identifier}
        )
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
                    "dictation stream fouled, resetting and retrying once: %s",
                    summary(error),
                )
                mlx.core.set_default_stream(mlx.core.new_stream(mlx.core.gpu))
                text = transcribe(samples)
            response_queue.put(("text", request_identifier, text))
        except BaseException as error:  # noqa: BLE001 — the caller is owed an answer, including a bad one
            logger.exception(
                "dictation transcription failed", extra={"request": request_identifier}
            )
            response_queue.put(("failed", request_identifier, summary(error)))
        finally:
            try:
                mlx.core.clear_cache()
            except Exception:  # noqa: BLE001 — a cache that will not clear is not a failed request
                logger.debug("could not clear the MLX cache", exc_info=True)


# What the model is doing, as the interface needs to know it, with loading a state rather than a wait.
STATE_IDLE = "idle"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_FAILED = "failed"


class SpeechTranscriber:
    """Owns the worker process and replaces it when it stops answering, loading off to one side."""

    def __init__(self, model_identifier: str, timing) -> None:
        self._model_identifier = model_identifier
        # The timing section, held rather than unpacked, so a replaced worker uses the limits in force now.
        self._timing = timing
        self._context = multiprocessing.get_context("spawn")
        self._process: Optional[Any] = None
        self._requests: Optional[Any] = None
        self._responses: Optional[Any] = None
        # Two locks on purpose, so a status read never queues behind a load that is still downloading.
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
            # A worker that died between transcriptions leaves the state stale, so the process is what wins.
            if self._state == STATE_READY and not (
                self._process is not None and self._process.is_alive()
            ):
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
        """Begin loading the model if it is not already loading or loaded, returning immediately."""
        with self._state_lock:
            if self._closed or self._state in (STATE_LOADING, STATE_READY):
                return
            self._state = STATE_LOADING
            self._failure = ""
            self._settled.clear()
        self._loader = threading.Thread(
            target=self._load, name="langmesh-dictation-loader", daemon=True
        )
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
        """Bring a worker up and wait for it to report the model is loaded, without a deadline."""
        self._stop_process()
        self._requests = self._context.Queue()
        self._responses = self._context.Queue()
        self._process = self._context.Process(
            target=_worker_main,
            args=(self._requests, self._responses, self._model_identifier, os.getpid()),
            name="langmesh-dictation",
            daemon=True,
        )
        self._process.start()
        while True:
            # The queue is read before liveness is judged, since a failing worker writes why and then exits.
            try:
                kind, reason, summary = self._responses.get(timeout=0.2)
            except queue.Empty:
                if self._closed:
                    self._stop_process()
                    raise DictationUnavailable("Dictation is shutting down.")
                if not self._process.is_alive():
                    status = self._process.exitcode
                    self._stop_process()
                    # The exit status, because it separates a worker that could not load the model from one that never ran.
                    logger.error(
                        "the dictation worker exited before reporting (status %s); it may not have started at all",
                        status,
                    )
                    raise DictationUnavailable(
                        f"The dictation model could not be started (worker exited: {status}). If the daemon log has no traceback from the worker, it never ran."
                    )
                continue
            if kind == "ready":
                logger.info("dictation model loaded", extra={"model": self._model_identifier})
                return
            self._stop_process()
            # The worker already logged the traceback, so what crosses the queue is the reason as a value.
            logger.error("dictation worker failed to start: %s (%s)", reason, summary)
            if reason == STARTUP_MISSING_PACKAGE:
                raise DictationUnavailable(
                    "Dictation needs the `parakeet-mlx` package, which is not installed in this environment. Run `uv sync` in the LangMesh repository and restart the daemon."
                )
            raise DictationUnavailable(
                "The dictation model could not be loaded — the download may have failed. Check the connection and try again."
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
        """Transcribe one recording, blocking, waiting out a load rather than refusing."""
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
                    # One line and no traceback, since the worker logged that where its frames still mean something.
                    logger.error("dictation transcription failed: %s", detail)
                    last_failure = "the transcription failed"
                    break
                else:
                    last_failure = "the transcription timed out"
                # Whatever went wrong, this worker is not to be trusted with the retry, though the audio is.
                logger.warning(
                    "dictation attempt %d of %d failed (%s), replacing the worker",
                    attempt + 1,
                    attempts,
                    last_failure,
                )
                self._stop_process()
                self._set_state(STATE_IDLE)
            raise DictationUnavailable(f"Could not transcribe the recording — {last_failure}.")

    def close(self) -> None:
        # Flagged before the lock is taken, since a load in flight holds it and closing is meant to end that.
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
