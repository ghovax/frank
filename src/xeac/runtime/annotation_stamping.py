"""Stamp artifact-image annotations onto the image itself for vision models.

An ``artifact_image_annotations`` data part carries the structured facts — each
annotation's ``sequence`` number, comment, and position — but a vision-capable
model understands the feedback far better when it can *see* where the marks
land. This module renders a copy of the annotated image with a numbered circle
badge at every annotation position; the badge numbers are the same ``sequence``
values the JSON metadata uses, so the model correlates "annotation 2 says …"
with the visible "2" on the image.

Everything here is synchronous, pure image work (Pillow); the executor calls it
off-loop via ``asyncio.to_thread`` per the server's blocking-work discipline.
The stamped copy is harness machinery: it is inlined into the turn silently and
never narrated to the user.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any, Optional
from xeac.base.tuning import Limit, active_tuning

# Sources the stamper can read pixels from: a local file path (the common case —
# artifact images reference files on the server) or an inline data URI.
# Remote http(s) sources are deliberately not fetched here: turn assembly must
# not block on the network, and the structured annotation JSON still gives the
# model the full facts for those.
_DATA_URI_PREFIX = "data:"

# Longest image side after downscaling. Keeps the inlined copy cheap in tokens
# and bytes while staying comfortably readable; annotation positions are placed
# from ratios, so scaling costs no precision.

# Hard ceiling on the encoded stamped image, mirroring the attachment inlining
# cap — anything larger is dropped rather than ballooning the request.
_MAXIMUM_STAMPED_BYTES = 20 * 1024 * 1024

ANNOTATIONS_PART_KIND = "artifact_image_annotations"


def _load_image_bytes(source: str) -> Optional[bytes]:
    """Read the raw image bytes behind an annotation payload's ``image.source``."""
    source = (source or "").strip()
    if not source:
        return None
    if source.startswith(_DATA_URI_PREFIX):
        _, _, encoded = source.partition(",")
        try:
            return base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError):
            return None
    if source.startswith(("http://", "https://")):
        return None
    path = source[len("file://"):] if source.startswith("file://") else source
    try:
        return Path(path).expanduser().read_bytes()
    except OSError:
        return None


def _badge_positions(annotations: list[dict], width: int, height: int) -> list[tuple[int, int, str]]:
    """Each annotation's badge center (clamped inside the image) and label.

    Positions come from the normalized ratios so they survive any resizing;
    normalized grid coordinates are the fallback."""
    positions: list[tuple[int, int, str]] = []
    for index, annotation in enumerate(annotations, start=1):
        if not isinstance(annotation, dict):
            continue
        position = annotation.get("position") or {}
        x_ratio, y_ratio = position.get("x_ratio"), position.get("y_ratio")
        if isinstance(x_ratio, (int, float)) and isinstance(y_ratio, (int, float)):
            center_x, center_y = x_ratio * width, y_ratio * height
        else:
            center_x = float(position.get("x") or 0) / _COORDINATE_GRID_MAXIMUM * width
            center_y = float(position.get("y") or 0) / _COORDINATE_GRID_MAXIMUM * height
        label = str(annotation.get("sequence") or index)
        positions.append((round(center_x), round(center_y), label))
    return positions


# Badges are drawn on a transparent overlay rendered at this multiple of the
# image size, then LANCZOS-downscaled and composited — Pillow's primitives are
# not antialiased, so supersampling is what keeps discs and digits smooth.
_BADGE_SUPERSAMPLE = 4


# The frontend's annotation marker, replicated exactly (see AnnotatableImage in
# web/src/components/tool-views/index.tsx): a 30px circle, blue fill, white bold
# ~15px number centered (half the diameter). All the numbers below are expressed
# relative to that 30px reference so the marker keeps the same proportions when it
# scales up on larger images.
_MARKER_REFERENCE_DIAMETER = 30
# Inner padding kept clear around the centered number (was the old border ring).
_MARKER_TEXT_PADDING_FRACTION = 2 / _MARKER_REFERENCE_DIAMETER
_MARKER_FONT_FRACTION = 15 / _MARKER_REFERENCE_DIAMETER
# The disc fill: Chakra's `blue.solid` (blue.600, #2563eb) — the same blue the
# frontend marker and the active version badge use, with white text on top.
_MARKER_FILL = (37, 99, 235, 255)


def stamp_annotations(image_bytes: bytes, annotations: list[dict]) -> Optional[bytes]:
    """Render numbered markers onto a copy of the image; returns PNG bytes.

    The markers are styled identically to the ones the user sees in the artifacts panel
    panel — blue disc, white centered number — so the stamped copy looks exactly like
    the annotated artifact."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception:
        return None
    image = image.convert("RGBA")
    if max(image.size) > active_tuning().amount(Limit.STAMPED_IMAGE_SIDE):
        image.thumbnail((active_tuning().amount(Limit.STAMPED_IMAGE_SIDE), active_tuning().amount(Limit.STAMPED_IMAGE_SIDE)), Image.LANCZOS)
    width, height = image.size
    diameter = max(_MARKER_REFERENCE_DIAMETER, round(min(width, height) * 0.066))
    radius = diameter // 2

    scale = _BADGE_SUPERSAMPLE
    scaled_radius = radius * scale
    text_padding = max(1, round(diameter * _MARKER_TEXT_PADDING_FRACTION)) * scale

    marker_layer = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(marker_layer)

    def _fitted_font(label: str):
        """The frontend's ~15px-at-30px font size, shrunk further only when a
        multi-digit label would not fit inside the disc."""
        size = round(diameter * _MARKER_FONT_FRACTION) * scale
        try:
            font = ImageFont.load_default(size=size)
            while size > scale * 4 and draw.textlength(label, font=font) > (scaled_radius - text_padding) * 1.5:
                size = round(size * 0.85)
                font = ImageFont.load_default(size=size)
            return font
        except TypeError:  # older Pillow without sized default fonts
            return ImageFont.load_default()

    for center_x, center_y, label in _badge_positions(annotations, width, height):
        font = _fitted_font(label)
        # Clamp the marker fully inside the canvas so edge annotations stay visible.
        center_x = min(max(center_x, radius + 2), width - radius - 2) * scale
        center_y = min(max(center_y, radius + 2), height - radius - 2) * scale
        bounds = (
            center_x - scaled_radius, center_y - scaled_radius,
            center_x + scaled_radius, center_y + scaled_radius,
        )
        # A plain blue disc filling the whole circle — no border ring, no shadow.
        draw.ellipse(bounds, fill=_MARKER_FILL)
        draw.text((center_x, center_y), label, fill=(255, 255, 255, 255), font=font, anchor="mm")

    overlay = marker_layer.resize((width, height), Image.LANCZOS)
    image = Image.alpha_composite(image, overlay)

    output = io.BytesIO()
    image.save(output, format="PNG")
    stamped = output.getvalue()
    if len(stamped) > _MAXIMUM_STAMPED_BYTES:
        return None
    return stamped


# The model-facing coordinate convention: integer positions on a fixed 0–999
# grid (origin top-left), the same on every axis regardless of the image's
# pixel dimensions. One clean coordinate system instead of a mix of absolute
# pixels and float ratios.
_COORDINATE_GRID_MAXIMUM = 999


def _grid_value(ratio: float) -> int:
    return min(_COORDINATE_GRID_MAXIMUM, max(0, round(ratio * _COORDINATE_GRID_MAXIMUM)))


def normalize_annotation_payloads(structured_payloads: list[dict]) -> list[dict]:
    """The model-facing copy of the turn's data parts, with every annotation's
    position rewritten to the normalized 0–999 grid.

    Only ``artifact_image_annotations`` payloads are touched (deep-copied, never
    mutated — the originals still carry the raw ratios the badge stamper and the
    UI use); everything else passes through unchanged. Each annotation keeps its
    ``sequence``, ``comment``, and natural ``image_size``. The grid convention
    itself is documented once, in the system prompt — not restated per payload."""
    normalized: list[dict] = []
    for payload in structured_payloads:
        if not isinstance(payload, dict) or payload.get("kind") != ANNOTATIONS_PART_KIND:
            normalized.append(payload)
            continue
        rewritten = dict(payload)
        rewritten_annotations: list[dict] = []
        for annotation in payload.get("annotations") or []:
            if not isinstance(annotation, dict):
                rewritten_annotations.append(annotation)
                continue
            entry = dict(annotation)
            position = annotation.get("position") or {}
            x_ratio, y_ratio = position.get("x_ratio"), position.get("y_ratio")
            if not (isinstance(x_ratio, (int, float)) and isinstance(y_ratio, (int, float))):
                x_ratio = float(position.get("x") or 0) / _COORDINATE_GRID_MAXIMUM
                y_ratio = float(position.get("y") or 0) / _COORDINATE_GRID_MAXIMUM
            entry["position"] = {"x": _grid_value(float(x_ratio)), "y": _grid_value(float(y_ratio))}
            rewritten_annotations.append(entry)
        rewritten["annotations"] = rewritten_annotations
        normalized.append(rewritten)
    return normalized


def annotation_image_blocks(structured_payloads: list[dict]) -> list[dict[str, Any]]:
    """OpenAI-shaped ``image_url`` content blocks for every annotation payload in
    the turn whose image can be read and stamped. One block per annotated image,
    badge numbers matching the payload's ``sequence`` values. Payloads whose
    source cannot be read (a remote URL, a missing file) simply contribute no
    block — the structured JSON already carries their facts."""
    blocks: list[dict[str, Any]] = []
    for payload in structured_payloads:
        if not isinstance(payload, dict) or payload.get("kind") != ANNOTATIONS_PART_KIND:
            continue
        annotations = payload.get("annotations") or []
        if not annotations:
            continue
        image_info = payload.get("image") or {}
        image_bytes = _load_image_bytes(str(image_info.get("source") or ""))
        if image_bytes is None:
            continue
        stamped = stamp_annotations(image_bytes, list(annotations))
        if stamped is None:
            continue
        encoded = base64.b64encode(stamped).decode("ascii")
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{encoded}"},
        })
    return blocks


__all__ = [
    "annotation_image_blocks",
    "normalize_annotation_payloads",
    "stamp_annotations",
    "ANNOTATIONS_PART_KIND",
]
