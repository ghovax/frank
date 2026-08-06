"""Dictation routes: the opt-in toggle, and turning raw mono float32 samples into text."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from langmesh.commons import state
from langmesh.commons.services.broadcast import _publish_broadcast
from langmesh.commons.services.settings import _persist_configuration
from langmesh.protocol.dtos import DictationUpdateRequest

logger = logging.getLogger(__name__)

router = APIRouter()

# The one transcriber this daemon owns, built on first use, since nothing else has business with it.
_transcriber = None
_transcriber_lock = asyncio.Lock()

#: A ceiling on one request, in samples: ten minutes, far past anything dictated into a composer.
MAXIMUM_SAMPLES = 16000 * 60 * 10


def _shutdown_transcriber() -> None:
    """Drop the worker, if there is one. Called when dictation is turned off and at shutdown."""
    global _transcriber
    transcriber, _transcriber = _transcriber, None
    if transcriber is not None:
        transcriber.close()


@router.get("/dictation")
async def dictation_status(prepare: bool = False):
    """Whether dictation is on, which model it uses, and what that model is doing."""
    assert state.global_configuration is not None
    dictation = state.global_configuration.dictation
    from langmesh.dictation.transcriber import STATE_IDLE

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
    """Persist and apply the toggle, releasing the worker at once since it holds a model in wired memory."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        await _persist_configuration(dictation_enabled=request.enabled)
        state.global_configuration.dictation.enabled = request.enabled
        if not request.enabled:
            await asyncio.to_thread(_shutdown_transcriber)
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "enabled": state.global_configuration.dictation.enabled}


async def _ensure_transcriber():
    """The transcriber, built on first use, never at import: a person who does not dictate pays nothing."""
    global _transcriber
    async with _transcriber_lock:
        if _transcriber is None:
            from langmesh.dictation.transcriber import SpeechTranscriber

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
            headers={"X-LangMesh-Reason": "dictation_disabled"},
        )
    body = await request.body()
    if len(body) < 4:
        raise HTTPException(status_code=400, detail="The recording was empty.")
    if len(body) // 4 > MAXIMUM_SAMPLES:
        raise HTTPException(status_code=413, detail="That recording is too long to transcribe.")

    from langmesh.dictation.transcriber import DictationUnavailable

    transcriber = await _ensure_transcriber()

    def run() -> str:
        # A copy, not a view: the buffer is the request body, and the samples cross a process boundary after it.
        import numpy

        samples = numpy.frombuffer(body, dtype="<f4").astype("float32")
        return transcriber.transcribe(samples)

    try:
        text = await asyncio.to_thread(run)
    except DictationUnavailable as error:
        # 503 rather than 500: the request was fine and the machine could not serve it.
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"text": text}


__all__ = ["router"]
