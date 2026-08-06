"""Preference routes: how the interface should look, and where it should open."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from langmesh.commons.services.broadcast import _publish_broadcast
from langmesh.commons.services.preferences import preferences_payload, update_preferences
from langmesh.protocol.dtos import InterfacePreferencesUpdateRequest

router = APIRouter()


@router.get("/preferences")
async def read_preferences():
    """Everything the interface remembers, as one object."""
    return await asyncio.to_thread(preferences_payload)


@router.post("/preferences")
async def write_preferences(request: InterfacePreferencesUpdateRequest):
    """Change one or more of them and answer with all of them, broadcasting so every client follows."""
    try:
        stored = await asyncio.to_thread(update_preferences, request.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _publish_broadcast({"type": "preferences_changed"})
    return stored
