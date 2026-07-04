"""Fan-out enrichment of a parsed document.

After Datalab returns the Marker block tree, three enrichments run — all as
bounded, parallel fan-out over the blocks:

* **Citations** — an LLM reads each text block and returns the citation marker
  numbers it references ([12], superscripts, ranges expanded). No regex: the model
  handles the messy real formats, with a retry and an empty-list fallback.
* **Figures** — each figure block's base64 image is described by a vision model, so
  the picture becomes searchable text.
* **Aspects** — every content block is classified into a normalized information type
  (method / result / limitation / …) from a controlled vocabulary, so the corpus can
  be sliced by what a passage IS across papers, not just by keyword.
* **References** — the bibliography is split into numbered entries (marker number +
  verbatim text) and stored **unresolved**. Turning a reference into a DOI is
  *discovery* — a separate concern that belongs to a search engine (e.g. the
  ``scholar`` package), not to this store. Whoever composes the two resolves the raw
  strings and links the cited work back with ``link_reference``.

Each call uses **structured outputs via function-calling**, the same mechanism the
harness uses: a Pydantic schema is bound to the model as a tool (its JSON schema is the
tool's parameters), the model is asked to call it, and the tool-call arguments are
validated against the schema — rejected and retried if off-shape, never merely asked to
"respond in JSON" and trusted. Tool-calling is chosen over strict ``json_schema``
``response_format`` because it is portable (Qwen/DashScope and most OpenAI-compatible
gateways support tool-calling but not native structured outputs). Prompts live in
``prompts/`` and are loaded by name, never inlined; they describe the task while the
schema defines the structure. Nothing sent to the model is truncated — the full
block/bibliography text is passed. Everything here is idempotent and side-effect free;
the caller persists the results. The model is reached through ``litellm`` using
``BLACKBOARD_MODEL`` (plus the provider key/base URL); with no model configured the LLM
enrichments are skipped and parsing/indexing still work.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from .board_schema import IMAGE_BLOCK_TYPES, normalize_aspect
from .prompts import load_prompt


# Bounds concurrent model/API calls during a document's fan-out, so a paper with
# hundreds of blocks does not open hundreds of sockets at once.
_MAXIMUM_CONCURRENCY = 8
_RETRIES = 3


# The expected output shape of each enrichment call. Each is bound to the model as a
# function tool (its JSON schema is the tool's parameters), so the model is constrained
# to this structure — not merely asked to "respond in JSON" and trusted to comply.

class CitationMarkers(BaseModel):
    """The citation marker numbers a passage references."""
    marker_numbers: list[int] = Field(
        description="Every citation marker number cited in the passage, ranges expanded (14-16 -> 14, 15, 16); empty if none."
    )


class ReferenceEntry(BaseModel):
    """One numbered bibliography entry, kept verbatim."""
    marker_number: int = Field(description="The entry's number in the reference list.")
    raw_string: str = Field(description="The entry's full text, verbatim.")


class ReferenceList(BaseModel):
    """A paper's reference section, split into numbered entries."""
    references: list[ReferenceEntry] = Field(description="Every entry in the reference list, in order.")


class AnchorAspect(BaseModel):
    """The single information aspect a passage conveys."""
    aspect: str = Field(description="One label from the controlled vocabulary of information aspects.")


_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


def _model_config() -> dict[str, Any]:
    return {
        "model": os.environ.get("BLACKBOARD_MODEL", "").strip(),
        "api_key": os.environ.get("BLACKBOARD_MODEL_API_KEY") or os.environ.get("OPENCODE_API_KEY") or None,
        "api_base": os.environ.get("BLACKBOARD_MODEL_BASE_URL") or None,
    }


def _vision_model_config() -> dict[str, Any]:
    """The vision-language model used to describe figures. Set ``BLACKBOARD_VISION_MODEL``
    (plus its own ``…_API_KEY`` / ``…_BASE_URL``) to a vision-capable model — figure
    description sends the actual image, so a text-only ``BLACKBOARD_MODEL`` cannot do it.
    When the vision vars are unset, this falls back to the text model config (which only
    works if that model happens to be vision-capable)."""
    model = os.environ.get("BLACKBOARD_VISION_MODEL", "").strip()
    if not model:
        return _model_config()
    return {
        "model": model,
        "api_key": os.environ.get("BLACKBOARD_VISION_MODEL_API_KEY") or os.environ.get("OPENCODE_API_KEY") or None,
        "api_base": os.environ.get("BLACKBOARD_VISION_MODEL_BASE_URL") or None,
    }


def _tool_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Render a Pydantic model as a function tool (its JSON schema is the tool's
    parameters). This is the harness's structured-output mechanism: bind the schema as
    a tool, let the model call it, and validate the call arguments — tool-calling is
    supported broadly (including Qwen/DashScope) where strict ``json_schema``
    ``response_format`` is not."""
    return {
        "type": "function",
        "function": {
            "name": schema.__name__,
            "description": (schema.__doc__ or "").strip(),
            "parameters": schema.model_json_schema(),
        },
    }


async def _complete_structured(system_prompt: str, user_content: str, schema: type[_SchemaT]) -> _SchemaT | None:
    """One completion whose output is constrained to ``schema`` via function-calling,
    with a small retry. The schema is bound as a tool and the model is asked to call
    it (``tool_choice="auto"`` — a forced tool choice is rejected by some providers,
    per the harness); the tool-call arguments are then validated against the model, so
    a malformed or off-shape reply is rejected and retried, not trusted. This mirrors
    the harness's ``bind_tools`` + manual-parse pattern. ``user_content`` is passed in
    full — never truncated. Returns ``None`` when no model is configured or every
    attempt fails, so the caller degrades gracefully."""
    configuration = _model_config()
    if not configuration["model"]:
        return None
    import litellm

    tool = _tool_for(schema)
    for _attempt in range(_RETRIES):
        try:
            response = await litellm.acompletion(
                model=configuration["model"],
                api_key=configuration["api_key"],
                api_base=configuration["api_base"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                tools=[tool],
                tool_choice="auto",
                temperature=0,
            )
            tool_calls = getattr(response["choices"][0]["message"], "tool_calls", None)
            if not tool_calls:
                await asyncio.sleep(0.5)
                continue
            return schema.model_validate_json(tool_calls[0].function.arguments)
        except Exception:  # noqa: BLE001 — a transient model/parse/validation error must not sink the document
            await asyncio.sleep(0.5)
    return None


async def extract_citation_markers(block_html: str, block_text: str) -> list[int]:
    """The citation marker numbers a text block references. LLM-only by design —
    ranges and mixed styles defeat a regex; a well-prompted model handles them."""
    passage = (block_html or block_text or "").strip()
    if not passage:
        return []
    result = await _complete_structured(load_prompt("citation_markers"), passage, CitationMarkers)
    if result is None:
        return []
    marker_numbers: list[int] = []
    for number in result.marker_numbers:
        if number > 0 and number not in marker_numbers:
            marker_numbers.append(number)
    return marker_numbers


async def classify_aspect(section: str, content: str) -> str:
    """Classify one passage into a normalized information aspect (method, result,
    limitation, …). The section hierarchy is passed as a hint. Full content, no
    truncation; falls back to ``other`` with no model or on failure."""
    passage = (content or "").strip()
    if not passage:
        return "other"
    result = await _complete_structured(load_prompt("anchor_aspect", section=section or "(unknown)"), passage, AnchorAspect)
    if result is None:
        return "other"
    return normalize_aspect(result.aspect)


async def parse_reference_list(bibliography_json: str) -> list[dict[str, Any]]:
    """Split a paper's reference/bibliography section into numbered entries. The input
    is a JSON array of the reference-section text fragments (block texts), passed to
    the model in full — nothing is truncated."""
    payload = (bibliography_json or "").strip()
    if not payload or payload == "[]":
        return []
    result = await _complete_structured(load_prompt("reference_list"), payload, ReferenceList)
    if result is None:
        return []
    return [
        {"marker_number": entry.marker_number, "raw_string": entry.raw_string.strip()}
        for entry in result.references
    ]


async def describe_figure(image_base64: str, mime: str = "image/png", caption: str = "") -> str:
    """A vision-model description of a figure so a picture becomes searchable text. The
    actual image is sent to a vision model (see ``_vision_model_config``); the caption,
    labelled as such, is injected into the prompt as context."""
    configuration = _vision_model_config()
    if not configuration["model"] or not image_base64:
        return ""
    import litellm

    data_url = image_base64 if image_base64.startswith("data:") else f"data:{mime or 'image/png'};base64,{image_base64}"
    caption_text = caption.strip() or "(no caption was provided in the paper)"
    instruction = load_prompt("figure_description", caption=caption_text)
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    for _attempt in range(_RETRIES):
        try:
            response = await litellm.acompletion(
                model=configuration["model"],
                api_key=configuration["api_key"],
                api_base=configuration["api_base"],
                messages=messages,
                temperature=0,
            )
            return (response["choices"][0]["message"]["content"] or "").strip()
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
    return ""


def _is_reference_block(block: dict[str, Any]) -> bool:
    block_type = str(block.get("block_type") or "")
    if block_type == "TableOfContents":
        return False
    if block_type == "Reference":
        return True
    section = " ".join(str(value) for value in (block.get("section_hierarchy") or {}).values()).lower()
    text = (block.get("text") or "").lower()
    return "reference" in section or "bibliography" in section or text.startswith(("references", "bibliography"))


async def enrich_document(parsed: dict[str, Any], *, semaphore: asyncio.Semaphore | None = None) -> dict[str, Any]:
    """Run all three enrichments over a parsed document, in bounded parallel.

    Returns derived facts keyed to the blocks they came from: ``citations_by_block``
    (block_id -> [marker numbers]), ``figure_descriptions`` (``block_id::image_key``
    -> text), ``aspects_by_block`` (block_id -> information aspect), and ``references``
    (the parsed bibliography, left unresolved — resolving a reference to a DOI is
    discovery, done externally and linked back later)."""
    limiter = semaphore or asyncio.Semaphore(_MAXIMUM_CONCURRENCY)
    blocks = parsed.get("blocks", [])
    images = parsed.get("images", {}) or {}

    async def guarded(coroutine):
        async with limiter:
            return await coroutine

    # Citations: one call per text block that could carry markers.
    text_blocks = [
        block for block in blocks
        if block.get("evidence_modality") == "text" and (block.get("html") or block.get("text"))
    ]
    citation_results = await asyncio.gather(
        *[guarded(extract_citation_markers(block.get("html", ""), block.get("text", ""))) for block in text_blocks],
        return_exceptions=True,
    )
    citations_by_block: dict[str, list[int]] = {}
    for block, markers in zip(text_blocks, citation_results):
        if isinstance(markers, list) and markers:
            citations_by_block[str(block.get("block_id"))] = markers

    # Figures: describe each image block via the vision model.
    figure_owner_blocks: list[tuple[dict[str, Any], str]] = []
    figure_tasks = []
    for block in blocks:
        if str(block.get("block_type")) not in IMAGE_BLOCK_TYPES:
            continue
        for image_key in block.get("image_keys") or []:
            image_base64 = images.get(image_key)
            if not image_base64:
                continue
            figure_owner_blocks.append((block, image_key))
            figure_tasks.append(guarded(describe_figure(image_base64, "image/png")))
    figure_results = await asyncio.gather(*figure_tasks, return_exceptions=True)
    figure_descriptions: dict[str, str] = {}
    for (block, image_key), description in zip(figure_owner_blocks, figure_results):
        if isinstance(description, str) and description:
            figure_descriptions[f"{block.get('block_id')}::{image_key}"] = description

    # Aspects: classify every content-bearing block into a normalized information type
    # (method / result / limitation / …) so the corpus is sliceable by what a passage
    # IS, across papers. A figure is classified from its generated description; every
    # other block from its own text.
    aspect_targets: list[tuple[str, str, str]] = []  # (block_id, section, content)
    for block in blocks:
        block_id = str(block.get("block_id") or "")
        if not block_id:
            continue
        section = " > ".join(str(value) for value in (block.get("section_hierarchy") or {}).values())
        if str(block.get("block_type")) in IMAGE_BLOCK_TYPES:
            content = next(
                (figure_descriptions[f"{block_id}::{key}"] for key in (block.get("image_keys") or [])
                 if figure_descriptions.get(f"{block_id}::{key}")),
                "",
            )
        else:
            content = str(block.get("text") or block.get("html") or "")
        if content.strip():
            aspect_targets.append((block_id, section, content))
    aspect_results = await asyncio.gather(
        *[guarded(classify_aspect(section, content)) for (_block_id, section, content) in aspect_targets],
        return_exceptions=True,
    )
    aspects_by_block: dict[str, str] = {}
    for (block_id, _section, _content), aspect in zip(aspect_targets, aspect_results):
        if isinstance(aspect, str) and aspect:
            aspects_by_block[block_id] = aspect

    # References: split the bibliography into numbered entries, stored unresolved.
    # The reference-section blocks go to the model as a JSON array of text fragments
    # (structured, boundaries preserved) rather than a newline-smushed blob. Resolving
    # a raw entry to a DOI is discovery — not this package's job — so it is resolved
    # externally (with a search engine) and the cited work is linked back later via
    # ``link_reference``.
    reference_fragments = [str(block.get("text") or "") for block in blocks if _is_reference_block(block)]
    reference_entries = await parse_reference_list(json.dumps(reference_fragments, ensure_ascii=False))
    references = [
        {
            "marker_number": entry["marker_number"],
            "raw_string": entry["raw_string"],
            "external_id": "",
            "resolved_title": "",
            "resolution_status": "unresolved",
        }
        for entry in reference_entries
    ]

    return {
        "citations_by_block": citations_by_block,
        "figure_descriptions": figure_descriptions,
        "aspects_by_block": aspects_by_block,
        "references": references,
    }
