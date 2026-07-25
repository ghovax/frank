"""Sessions routes."""

from __future__ import annotations
from fastapi import APIRouter
from xeac.daemon.persistence.database import SessionRecord
from xeac.base.paths import uploads_directory
import asyncio
import re
from xeac.protocol.dtos import (
    SessionDraftRequest,
)
from xeac.daemon import state
from xeac.daemon.services.broadcast import _publish_broadcast
from xeac.daemon.persistence.artifacts import _prune_session_artifacts
from xeac.daemon.services.sessions import _abort_pending_input, _remove_upload_file, _session_draft, _sessions_payload, _update_session_draft

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
    await _abort_pending_input(session_id)
    state._awaiting_input_contexts.discard(session_id)
    # A session's live state lives in its own process, so deleting it is a reap rather than a
    # per-executor teardown: the process goes, and with it the runtime, the resume pump, the
    # turn lock, and the conversation. Its rows are removed below either way.
    if state.lifecycle is not None:
        await state.lifecycle.reap(session_id, reason="session deleted")
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
    # Prune this session's artifact versions (shadow-git branches + index rows) before the
    # session record goes, so its locations can still be resolved for the branch delete.
    await asyncio.to_thread(_prune_session_artifacts, session_id)
    # The retention pass above already removed persisted conversation/lifecycle state;
    # finish by removing the sidebar record. teardown_context dropped the live copies.
    def _delete_record() -> bool:
        assert state.session_factory is not None
        database_session = state.session_factory()
        try:
            record = database_session.query(SessionRecord).filter(SessionRecord.id == session_id).first()
            if record is None:
                database_session.commit()
                return False
            database_session.delete(record)
            database_session.commit()
            return True
        finally:
            database_session.close()
    deleted = await asyncio.to_thread(_delete_record)
    _publish_broadcast({"type": "sessions_changed"})
    return {"status": "deleted" if deleted else "not_found", "session_id": session_id}
