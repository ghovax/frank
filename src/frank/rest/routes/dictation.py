"""Dictation routes: the opt-in toggle, and turning a recording into text.

The recording is made in the interface, because that is where the microphone and the person
are, and it arrives here as raw mono float32 at 16 kHz — not as a `.webm` or an `.m4a`. That
is deliberate and it is the reason this file needs no audio library: browsers disagree about
which container `MediaRecorder` produces, and accepting one would mean the daemon carrying a
decoder to open a file the browser had just finished encoding for no reason. The page already
holds the samples; it sends the samples.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from frank.hub import state
from frank.hub.services.broadcast import _publish_broadcast
from frank.hub.services.settings import _persist_configuration
from frank.protocol.dtos import DictationUpdateRequest

logger = logging.getLogger(__name__)

router = APIRouter()

# The one transcriber this daemon owns, built on first use. Held here rather than on the hub's
# state because nothing else in the process has any business with it, and a module global that
# one file reads is easier to reason about than a singleton four layers can reach.
_transcriber = None
_transcriber_lock = asyncio.Lock()

# A ceiling on what one request may carry, in samples. Dictation is short-form by nature; this
# is ten minutes, which is far past anything anybody dictates into a composer and well short of
# a payload that could exhaust the daemon's memory.
MAXIMUM_SAMPLES = 16000 * 60 * 10


def _shutdown_transcriber() -> None:
    """Drop the worker, if there is one. Called when dictation is turned off and at shutdown."""
    global _transcriber
    transcriber, _transcriber = _transcriber, None
    if transcriber is not None:
        transcriber.close()


@router.get("/dictation")
async def dictation_status(prepare: bool = False):
    """Whether dictation is on, which model it uses, and what that model is doing.

    `state` is the whole of how loading is presented: `idle` until something asks for the model,
    `loading` while it comes up — which on a first run includes fetching about a gigabyte — then
    `ready`, or `failed` with a sentence saying why. The composer polls this and shows the
    microphone arriving, rather than offering a button that would silently block on a download.

    `prepare=true` also starts the load. That is a GET with an effect, which is worth a word: it
    is idempotent (asking twice loads once), it creates nothing addressable, and the alternative
    — a separate POST the interface must remember to send — puts the same effect one round trip
    further from the question that motivates it."""
    assert state.global_configuration is not None
    dictation = state.global_configuration.dictation
    from frank.dictation.transcriber import STATE_IDLE

    transcriber = _transcriber
    if prepare and dictation.enabled:
        transcriber = await _ensure_transcriber()
        await asyncio.to_thread(transcriber.ensure_started)
    return {
        "enabled": dictation.enabled,
        "model": dictation.model,
        "state": transcriber.state if transcriber is not None else STATE_IDLE,
        "failure": transcriber.failure if transcriber is not None else "",
        "sample_rate": 16000,
    }


@router.post("/settings/dictation")
async def update_dictation(request: DictationUpdateRequest):
    """Persist and apply the opt-in dictation toggle.

    Turning it off releases the worker straight away rather than at some later tidy-up: it holds
    a model in wired GPU memory, and somebody who has just said they do not want dictation
    should not go on paying for it."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        await _persist_configuration(dictation_enabled=request.enabled)
        state.global_configuration.dictation.enabled = request.enabled
        if not request.enabled:
            await asyncio.to_thread(_shutdown_transcriber)
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "enabled": state.global_configuration.dictation.enabled}


async def _ensure_transcriber():
    """The transcriber, built on first use. Never at import: a person who does not dictate must
    not pay for a speech stack being importable."""
    global _transcriber
    async with _transcriber_lock:
        if _transcriber is None:
            from frank.dictation.transcriber import SpeechTranscriber

            assert state.global_configuration is not None
            dictation = state.global_configuration.dictation
            _transcriber = SpeechTranscriber(dictation.model, dictation.timing)
        return _transcriber


@router.post("/dictation/transcribe")
async def transcribe(request: Request):
    """Turn one recording into text. The body is raw little-endian float32 mono at 16 kHz."""
    assert state.global_configuration is not None
    if not state.global_configuration.dictation.enabled:
        raise HTTPException(
            status_code=409,
            detail="Dictation is off. Turn it on in Settings to transcribe on this machine.",
            headers={"X-Frank-Reason": "dictation_disabled"},
        )
    body = await request.body()
    if len(body) < 4:
        raise HTTPException(status_code=400, detail="The recording was empty.")
    if len(body) // 4 > MAXIMUM_SAMPLES:
        raise HTTPException(status_code=413, detail="That recording is too long to transcribe.")

    from frank.dictation.transcriber import DictationUnavailable

    transcriber = await _ensure_transcriber()

    def run() -> str:
        # numpy is already a dependency of everything under this, and a copy is taken rather
        # than a view: the buffer is the request body, which is released when this returns,
        # while the samples cross a process boundary after it.
        import numpy

        samples = numpy.frombuffer(body, dtype="<f4").astype("float32")
        return transcriber.transcribe(samples)

    try:
        text = await asyncio.to_thread(run)
    except DictationUnavailable as error:
        # 503 rather than 500: the request was fine, the machine could not serve it, and the
        # message says which part — a missing package, a failed download, a wedged worker.
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"text": text}


__all__ = ["router"]
