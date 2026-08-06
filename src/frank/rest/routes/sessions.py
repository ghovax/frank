"""Sessions routes."""

from __future__ import annotations
from fastapi import APIRouter
from frank.hub.database import SessionRecord, WorkspaceRecord
from frank.base.paths import uploads_directory
import asyncio
import re
from frank.protocol.dtos import (
    SessionDraftRequest,
)
from frank.hub import state
from frank.hub.services.broadcast import _publish_broadcast
from frank.hub.services.sessions import _remove_upload_file, _session_draft, _sessions_payload, _update_session_draft

router = APIRouter()

@router.get("/sessions")
async def list_sessions():
    return await asyncio.to_thread(_sessions_payload)


@router.get("/sessions/{session_id}/draft")
async def session_draft(session_id: str):
    return {"input_draft": await asyncio.to_thread(_session_draft, session_id)}


@router.put("/sessions/{session_id}/draft")
async def update_session_draft(session_id: str, request: SessionDraftRequest):
    await asyncio.to_thread(_update_session_draft, session_id, request.input_draft)
    return {"ok": True}


@router.get("/sessions/{session_id}/turns")
async def session_turns(session_id: str):
    """Every turn a session has had, with its history and artifacts, for replay."""
    assert state.turn_store is not None
    turns = await state.turn_store.turns_for_session(session_id)
    return {
        "turns": [
            turn.model_dump(by_alias=True, exclude_none=True, mode="json")
            for turn in turns
        ]
    }


@router.get("/sessions/{session_id}/turns/page")
async def session_turn_page(session_id: str, before_row_id: int | None = None, limit: int = 400):
    """A bounded replay page for fast session switching.

    Returns the newest persisted turn-history rows first on the initial call;
    pass ``before_row_id`` from the previous response to load older rows. This
    keeps long conversations interactive without waiting for the complete turn
    history to deserialize and cross the local HTTP boundary.
    """
    assert state.turn_store is not None
    page = await state.turn_store.turn_page_for_session(session_id, before_row_id=before_row_id, limit=limit)
    return {
        "turns": [
            turn.model_dump(by_alias=True, exclude_none=True, mode="json")
            for turn in page["turns"]
        ],
        "next_before_row_id": page["next_before_row_id"],
        "has_more": page["has_more"],
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Permanently delete a session and all its tasks. Aborts the context first."""
    # Settle any input-required pause and drop the awaiting-input marker; the pending
    # record and tasks are removed with the session below regardless.
    # A session's live state lives in its own process, so deleting it means *ending* it: the
    # process goes, and with it the runtime, the resume pump, the turn lock and the
    # conversation. Only the control plane can do that, and this surface deliberately cannot
    # reach the control plane — so it says what happened and the composition root decided who
    # listens. Without one, the rows below are still removed, which is the right behaviour for
    # a workspace served without a supervisor.
    await state.session_deleted(session_id)
    # Delete every task in the context from the task store, then reclaim any upload files
    # the session referenced that no surviving session still references (uploads are
    # content-addressed and may be shared, so only truly-orphaned files are removed).
    if state.turn_store is not None:
        uploads_root = str(uploads_directory())
        upload_pattern = re.compile(re.escape(uploads_root) + r"/[^\"\\]+")
        referenced_uploads: set[str] = set()
        for text in await state.turn_store.session_message_texts(session_id):
            referenced_uploads.update(upload_pattern.findall(text))
        # One call drops the context's tasks (head/history/artifacts) and its
        # conversation checkpoint — the single durable turn surface.
        await state.turn_store.delete_session(session_id)
        for path_string in referenced_uploads:
            if not await state.turn_store.any_history_references(path_string):
                await asyncio.to_thread(_remove_upload_file, path_string, uploads_root)
    # The retention pass above already removed persisted conversation and lifecycle state;
    # finish by removing the sidebar record. `teardown_context` dropped the live copies.
    def _delete_record() -> bool:
        assert state.session_factory is not None
        database_session = state.session_factory()
        try:
            record = database_session.query(SessionRecord).filter(SessionRecord.id == session_id).first()
            if record is None:
                database_session.commit()
                return False
            # A workspace pointing at a conversation that no longer exists would send the next
            # client that opened it looking for a session nothing can serve, so the pointer is
            # cleared here rather than being left for a reader to discover.
            database_session.query(WorkspaceRecord).filter(
                WorkspaceRecord.last_session_id == session_id
            ).update({WorkspaceRecord.last_session_id: ""})
            database_session.delete(record)
            database_session.commit()
            return True
        finally:
            database_session.close()
    deleted = await asyncio.to_thread(_delete_record)
    _publish_broadcast({"type": "sessions_changed"})
    return {"status": "deleted" if deleted else "not_found", "session_id": session_id}
