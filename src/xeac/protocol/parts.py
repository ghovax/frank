"""Turning messages into what the model reads, and runtime events into what the client renders.

Two directions meet here. Inbound: an A2A message's parts are unpacked into the turn's
inputs — prose, artifact interactions, structured attachments, and images inlined only when
the model can actually see them. Outbound: every runtime event becomes a validated wire
part, constructed as its Pydantic model at the emit site so a misnamed field is an error
here rather than invisible drift the schema generation can never catch.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from a2a.types import DataPart, FilePart, Part, TextPart

from xeac.base.message_content import content_block_metadata
from xeac.base.models import find_model
from xeac.base.paths import uploads_directory
from xeac.protocol.events import (
    ToolMetadata,
    ToolResultEvent,
    ToolStatus,
    WarningEvent,
    _EventBase,
)
from xeac.protocol.files import ingest_file_part
from xeac.protocol.metadata import ARTIFACT_EVENT_KIND, INPUT_RESPONSE_KIND, PART_KIND


def _artifact_event_payload(message) -> Optional[dict]:
    """The incoming artifact-interaction DataPart, if this turn carries one. Returns
    ``None`` when the message has no artifact event, so the caller falls back to the
    plain text input."""
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, DataPart) and root.data.get(PART_KIND) == ARTIFACT_EVENT_KIND:
            return {
                "artifact_id": root.data.get("artifactId", ""),
                "title": root.data.get("title", ""),
                "event": root.data.get("event", ""),
                "data": root.data.get("data"),
            }
    return None


# A message/send answering an input-required pause carries this DataPart kind, with the
# request id and the decision/answers, so an external A2A client can resolve a permission
# or question the spec-correct way rather than through the native REST side channel.


def _input_response_payload(message) -> Optional[dict]:
    """The input-required answer this message carries, or ``None``."""
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, DataPart) and root.data.get(PART_KIND) == INPUT_RESPONSE_KIND:
            return dict(root.data)
    return None


async def _ingest_incoming_file_parts(message) -> list[dict]:
    """Materialize every ``FilePart`` an inbound message carries into the upload store,
    returning attachment dicts so a file another agent sends reaches the model exactly like
    a local attachment."""
    attachments: list[dict] = []
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, FilePart):
            attachment = await ingest_file_part(part, uploads_directory().parent)
            if attachment is not None:
                attachments.append(attachment)
    return attachments


def _structured_data_payloads(message) -> list[dict]:
    """Return non-artifact DataPart payloads carried by the user turn."""
    payloads: list[dict] = []
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, DataPart):
            data = root.data
            if data.get(PART_KIND) == ARTIFACT_EVENT_KIND:
                continue
            payloads.append(dict(data))
    return payloads


# Attachments whose mime type starts with this are viewable by a vision model, so
# they are inlined into the turn as image content blocks. Everything else stays a
# text-only reference (path + metadata) the model opens with its file tools.
_INLINE_IMAGE_MIME_PREFIX = "image/"
# A generous ceiling on an inlined image so a huge upload cannot blow up the
# request (and the persisted conversation it becomes part of). Larger images are
# left as text references rather than inlined.
_MAXIMUM_INLINE_IMAGE_BYTES = 20 * 1024 * 1024


def _image_attachments(structured_payloads: list[dict]) -> list[dict]:
    """Every image attachment carried by the turn's ``attachments`` parts."""
    images: list[dict] = []
    for payload in structured_payloads:
        if payload.get(PART_KIND) != "attachments":
            continue
        for attachment in payload.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            if str(attachment.get("mime_type", "")).startswith(_INLINE_IMAGE_MIME_PREFIX):
                images.append(attachment)
    return images


def _all_attachments(structured_payloads: list[dict]) -> list[dict]:
    """Every file attachment carried by the turn (images and non-images alike)."""
    attachments: list[dict] = []
    for payload in structured_payloads:
        if payload.get(PART_KIND) != "attachments":
            continue
        for attachment in payload.get("attachments") or []:
            if isinstance(attachment, dict):
                attachments.append(attachment)
    return attachments


def _model_supports_vision(model_identifier: str) -> bool:
    if not model_identifier:
        return True
    model = find_model(model_identifier)
    if model is None:
        return True
    return model.vision


def _attachment_warning_event(image_count: int, model_identifier: str) -> WarningEvent:
    plural = "s" if image_count != 1 else ""
    return WarningEvent(
        code="image_metadata_only",
        title="Image attached as metadata only",
        message=(
            f"{image_count} image{plural} attached, but {model_identifier or 'the session model'} "
            "does not advertise vision support. The file metadata and path were provided to the model, "
            "but the image pixels were not inlined. Configure a vision-capable model for this agent if it needs to inspect the image directly."
        ),
    )


def _image_content_block(attachment: dict) -> Optional[dict]:
    """Build an OpenAI-shaped ``image_url`` content block from an attachment by
    reading the stored file and base64-encoding it as a data URI. Returns ``None``
    when the file is missing, unreadable, or too large to inline."""
    path = str(attachment.get("path") or "")
    if not path:
        return None
    mime_type = str(attachment.get("mime_type") or "application/octet-stream")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > _MAXIMUM_INLINE_IMAGE_BYTES:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}

# Each agent profile is served as its own A2A agent under this prefix.


def _text_part(text: str, block_identifier: str) -> Part:
    return Part(root=TextPart(
        text=text,
        metadata=content_block_metadata(block_identifier),
    ))


def _event_part(event: _EventBase) -> Part:
    """A validated wire-event ``Part``. Every client-facing event is constructed as its
    Pydantic model here, so a misnamed or missing field is a ``ValidationError`` at the emit
    site rather than an invisible wire drift the schema/TypeScript generation can never see
    (the emitter→contract edge, which raw-dict ``{kind, **fields}`` construction left on faith)."""
    return Part(root=DataPart(data=event.model_dump(mode="json")))


def _work_habits_acknowledgement_parts(task_identifier: str) -> tuple[Part, Part]:
    acknowledgement_identifier = f"work-habits-{task_identifier}"
    metadata = {
        "tool_name": "work_habits",
        "tool_call_id": acknowledgement_identifier,
    }
    return (
        _event_part(ToolCallEvent(
            tool_name="work_habits",
            tool_call_id=acknowledgement_identifier,
            arguments={"justification": "Loading your work habits"},
        )),
        _event_part(ToolResultEvent(
            tool_name="work_habits",
            tool_call_id=acknowledgement_identifier,
            status=ToolStatus.OK,
            display=None,
            metadata=ToolMetadata(**metadata),
        )),
    )


# Fields in a tool result that are guidance addressed to the model, or bulk payload it
# reads from the conversation — never something the transcript should render. They are
# stripped from `display` so the wire (live and on replay) carries only what the UI shows.
_MODEL_ONLY_RESULT_KEYS = frozenset({"hint", "note"})

# Per-tool heavy payloads the UI summarizes (a count, a range) rather than dumping: the
# model still reads them from the conversation; the client never needs the raw blob.
_HEAVY_RESULT_KEYS: dict[str, frozenset[str]] = {
    "search_code": frozenset({"matches"}),
    "read_file": frozenset({"content"}),
}


def _project_display(tool_name: str, result: object) -> object:
    """Trim a tool result down to the UI-facing view. The model reads the untrimmed
    result from the conversation; only this projection reaches the client, so
    model-directed guidance and bulk payloads never leak into the transcript."""
    if not isinstance(result, dict):
        return result
    drop = _MODEL_ONLY_RESULT_KEYS | _HEAVY_RESULT_KEYS.get(tool_name, frozenset())
    return {key: value for key, value in result.items() if key not in drop}


def _tool_result_part(tool_name: str, tool_call_id: str, result: object, status: str) -> Part:
    """The unified ``tool_result`` wire event for a root-agent tool. Lifecycle is the
    explicit ``status``; ``display`` is the projected payload the UI renders (the
    model-facing view travels only in the conversation). ``metadata`` here is the minimal
    display correlation — full timing rides the model envelope."""
    record = result if isinstance(result, dict) else {}
    return _event_part(ToolResultEvent(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status=ToolStatus(status),
        code=record.get("code"),
        display=_project_display(tool_name, result),
        metadata=ToolMetadata(tool_name=tool_name, tool_call_id=tool_call_id),
    ))
