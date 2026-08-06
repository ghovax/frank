from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence, cast

from langchain_core.messages import AIMessage, BaseMessage, convert_to_openai_messages
from langchain_core.messages.content import (
    ContentBlock,
    ReasoningContentBlock,
    TextContentBlock,
)


CONTENT_BLOCK_METADATA_KEY = "urn:langmesh:ext:content-block:v1"


@dataclass(frozen=True)
class MessageContentDelta:
    kind: Literal["text", "reasoning"]
    block_identifier: str
    text: str


def _block_identifier(block: ContentBlock) -> str:
    identifier = block.get("id")
    index = block.get("index")
    if identifier is None or index is None:
        raise ValueError("Model content blocks must carry both an id and an index.")
    return f"{block['type']}:{identifier}:{index}"


def content_block_metadata(block_identifier: str) -> dict[str, dict[str, str]]:
    if not block_identifier:
        raise ValueError("A2A text parts require a content-block identifier.")
    return {CONTENT_BLOCK_METADATA_KEY: {"id": block_identifier}}


def content_block_identifier(metadata: object) -> str | None:
    if not isinstance(metadata, dict):
        return None
    extension = metadata.get(CONTENT_BLOCK_METADATA_KEY)
    if not isinstance(extension, dict):
        return None
    identifier = extension.get("id")
    return str(identifier) if identifier else None


def content_blocks_to_message_content(
    content_blocks: Sequence[ContentBlock],
) -> list[str | dict[Any, Any]]:
    return [cast(dict[Any, Any], content_block) for content_block in content_blocks]


def message_content_deltas(message: BaseMessage) -> list[MessageContentDelta]:
    deltas: list[MessageContentDelta] = []
    for block in message.content_blocks:
        block_type = block.get("type")
        if block_type == "text":
            text_block = cast(TextContentBlock, block)
            text = text_block["text"]
            if text:
                deltas.append(MessageContentDelta("text", _block_identifier(block), text))
        elif block_type == "reasoning":
            reasoning_block = cast(ReasoningContentBlock, block)
            reasoning = reasoning_block.get("reasoning", "")
            if reasoning:
                deltas.append(MessageContentDelta("reasoning", _block_identifier(block), reasoning))
    return deltas


def _project_content_blocks(message: BaseMessage, block_type: str, content_field: str) -> str:
    text_blocks: list[ContentBlock] = [
        TextContentBlock(type="text", text=str(block.get(content_field, "")))
        for block in message.content_blocks
        if block.get("type") == block_type and block.get(content_field)
    ]
    if not text_blocks:
        return ""
    projected = convert_to_openai_messages(
        AIMessage(content_blocks=text_blocks),
        text_format="string",
        pass_through_unknown_blocks=False,
    )
    content = cast(dict[str, Any], projected).get("content")
    if not isinstance(content, str):
        raise TypeError("LangChain did not serialize text content blocks to a string.")
    return content


def message_text(message: BaseMessage) -> str:
    return _project_content_blocks(message, "text", "text")


def message_reasoning_text(message: BaseMessage) -> str:
    return _project_content_blocks(message, "reasoning", "reasoning")


#: Where a provider's own reasoning lives on a message, in the form that provider accepts back.
CARRIED_REASONING_KEYS = ("thinking_blocks", "reasoning_items")

#: Which model wrote the carried reasoning, since only that model can read it back.
REASONING_MODEL_KEY = "reasoning_model"


def carried_reasoning_for(message: BaseMessage, model: str) -> dict[str, Any]:
    """The provider-native reasoning on ``message``, if ``model`` is the one that can read it."""
    carried = getattr(message, "additional_kwargs", {}) or {}
    if carried.get(REASONING_MODEL_KEY) != model:
        return {}
    return {key: carried[key] for key in CARRIED_REASONING_KEYS if carried.get(key)}


def forget_carried_reasoning(message: BaseMessage) -> None:
    """Strip provider-native reasoning in place, where the conversation is no longer the one it was written into."""
    for key in (*CARRIED_REASONING_KEYS, REASONING_MODEL_KEY):
        message.additional_kwargs.pop(key, None)
