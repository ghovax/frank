"""Turning inbound messages into what the model reads, and runtime events into what the client renders."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from a2a.types import DataPart, FilePart, Part, TextPart

from frank.base.message_content import content_block_metadata
from frank.base.models import find_model
from frank.base.paths import uploads_directory
from frank.base.serialization import compact
from frank.protocol.events import (
    ToolCallEvent,
    ToolMetadata,
    ToolResultEvent,
    ToolStatus,
    WarningEvent,
    _EventBase,
)
from frank.protocol.files import ingest_file_part
from frank.protocol.metadata import (
    INPUT_RESPONSE_KIND,
    PART_KIND,
    part_payload,
    wrap_part_payload,
)


def _input_response_payload(message) -> Optional[dict]:
    """The input-required answer this message carries, or ``None``."""
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        payload = part_payload(root.data) if isinstance(root, DataPart) else {}
        if payload.get(PART_KIND) == INPUT_RESPONSE_KIND:
            return dict(payload)
    return None


async def _ingest_incoming_file_parts(message) -> list[dict]:
    """Materialize every inbound file part into the upload store, so a peer's file arrives like a local attachment."""
    attachments: list[dict] = []
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, FilePart):
            attachment = await ingest_file_part(part, uploads_directory().parent)
            if attachment is not None:
                attachments.append(attachment)
    return attachments


def _structured_data_payloads(message) -> list[dict]:
    """Return the DataPart payloads carried by the user turn."""
    payloads: list[dict] = []
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, DataPart):
            data = part_payload(root.data)
            payloads.append(dict(data))
    return payloads


# Attachments with this mime prefix are viewable by a vision model and so are inlined as image blocks.
_INLINE_IMAGE_MIME_PREFIX = "image/"
# A generous ceiling on an inlined image, since a huge one would blow up the persisted conversation.
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
    """An image content block built from a stored attachment, or `None` when it is missing or too large."""
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


def compose_turn_input(
    user_text: str, structured_payloads: list[dict], model_identifier: str,
) -> tuple[object, int]:
    """What the model reads for a turn carrying attachments, and how many images were left out."""
    # The metadata always rides along as text, so the model can act on the files whether or not it can see them.
    text_payload = compact({"text": user_text, "data_parts": structured_payloads})
    images = _image_attachments(structured_payloads)
    if not images:
        return text_payload, 0
    if not _model_supports_vision(model_identifier):
        return text_payload, len(images)
    blocks = [block for image in images if (block := _image_content_block(image)) is not None]
    if not blocks:
        return text_payload, 0
    return [{"type": "text", "text": text_payload}, *blocks], 0


def attachment_payload(attachments: list[dict]) -> dict:
    """The structured payload an ``attachments`` DataPart carries."""
    return {PART_KIND: "attachments", "attachments": attachments}


def _text_part(text: str, block_identifier: str) -> Part:
    return Part(root=TextPart(
        text=text,
        metadata=content_block_metadata(block_identifier),
    ))


def _event_part(event: _EventBase) -> Part:
    """A validated wire-event part, so a misnamed field fails at the emit site rather than in a client."""
    return Part(root=DataPart(data=wrap_part_payload(event.model_dump(mode="json"))))


def _work_habits_acknowledgement_parts(job_id: str) -> tuple[Part, Part]:
    acknowledgement_identifier = f"work-habits-{job_id}"
    metadata = {
        "tool_name": "work_habits",
        "tool_call_id": acknowledgement_identifier,
    }
    return (
        _event_part(ToolCallEvent(
            tool_name="work_habits",
            tool_call_id=acknowledgement_identifier,
            arguments={"explanation": "Loading your work habits"},
        )),
        _event_part(ToolResultEvent(
            tool_name="work_habits",
            tool_call_id=acknowledgement_identifier,
            status=ToolStatus.OK,
            display=None,
            metadata=ToolMetadata(**metadata),
        )),
    )


# Fields addressed to the model rather than to the transcript, stripped from what the client renders.
_MODEL_ONLY_RESULT_KEYS = frozenset({"hint", "note"})

# Per-tool heavy payloads the interface summarizes, which the model still reads from the conversation.
_HEAVY_RESULT_KEYS: dict[str, frozenset[str]] = {
    "search_code": frozenset({"matches"}),
    "read_file": frozenset({"content"}),
}


def _project_display(tool_name: str, result: object) -> object:
    """Trim a tool result down to the view the client renders."""
    if not isinstance(result, dict):
        return result
    drop = _MODEL_ONLY_RESULT_KEYS | _HEAVY_RESULT_KEYS.get(tool_name, frozenset())
    return {key: value for key, value in result.items() if key not in drop}


def _tool_result_part(tool_name: str, tool_call_id: str, result: object, status: str) -> Part:
    """The unified tool-result wire event, whose `display` is the projected payload the interface renders."""
    record = result if isinstance(result, dict) else {}
    return _event_part(ToolResultEvent(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        status=ToolStatus(status),
        code=record.get("code"),
        display=_project_display(tool_name, result),
        metadata=ToolMetadata(tool_name=tool_name, tool_call_id=tool_call_id),
    ))
