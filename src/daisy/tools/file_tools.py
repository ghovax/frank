r"""Concrete implementations of the file/search/edit tools.

These are plain functions (synchronous for filesystem work, async for HTTP) that
the agent runtime dispatches to from ``_execute_tool``. They mirror opencode's
tool semantics but follow this harness's convention of returning results as
``json.dumps({...})`` payloads with a ``code`` discriminator — the same shape
``bash`` and ``web_search`` already use. Errors are raised so the runtime's
tool-call wrapper surfaces them as ERROR events to the model.

Every function operates through a :class:`~daisy.locations.executor.LocationExecutor`,
so a call against a remote (SSH) location runs through exactly the same
result-building code as a local one — same ``cat -n`` formatting, staleness
hashes, whitespace/escape fallbacks, near-miss candidates, and syntax
validation. Only the IO primitives differ.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Parser

from markdownify import markdownify as _markdownify

from daisy.core.configuration import PromptLoader as _PromptLoader
from daisy.core.tuning import Limit, active_tuning, clip_to_tokens
from daisy.identifiers import new_id
from daisy.locations.executor import LocationExecutor

_VALIDATION_PROMPT_LOADER = _PromptLoader(Path(__file__).parent / "prompts")


# The read-line count, per-line clip, glob/grep result caps, and fetched-page clip are token
# budgets that scale with the live model context window; they are read per call from
# ``active_tuning()`` rather than fixed here.
# A fetch result shorter than this (after stripping) is treated as thin — an empty
# SPA shell or a block/challenge page — and the next engine is tried. A quality floor, not a
# token budget, so it stays fixed.
MINIMUM_USEFUL_FETCH_CHARS = 64

# Web-fetch engines, injected by the server on startup and on every settings change
# (mirroring the Exa client). Jina Reader is the always-available default (works
# keyless); the Firecrawl client is optional and only set when a key is configured.
_jina_api_key: str = ""
_firecrawl_client = None  # firecrawl.AsyncFirecrawl | None — kept untyped to avoid the import
# Optional proxy for the browser-impersonating direct tier and file downloads (for
# sites that block by IP). Empty means direct.
_proxy_url: str = ""


def set_jina_api_key(api_key: str) -> None:
    global _jina_api_key
    _jina_api_key = api_key or ""


def set_firecrawl_client(client) -> None:
    global _firecrawl_client
    _firecrawl_client = client


def set_proxy_url(proxy_url: str) -> None:
    global _proxy_url
    _proxy_url = proxy_url or ""

# read_file ingests these natively as images (pixels attached for vision models)
# instead of decoding their bytes as text. Matches the formats every routed
# vision provider accepts.
IMAGE_FILE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# Ceiling on an image attached into the conversation, mirroring the attachment
# inlining cap; larger files return metadata only.
MAXIMUM_INLINE_IMAGE_BYTES = 20 * 1024 * 1024


def _normalize_tool_escapes(value: str) -> str:
    """Collapse JSON escape sequences that survived the tool-call parsing pipeline.

    When a model sees previously-serialised tool call arguments in its conversation
    history (e.g. ``\"`` rendered as ``\\\"`` inside a JSON string), it may copy the
    escaped form literally into its own tool call.  By the time the arguments reach
    this handler they have already been through ``json.loads`` once, which should
    have unescaped them — but some model providers double-encode, and some models
    re-escape when reading from history.

    This is strictly a *fallback*: source code legitimately contains literal
    escape sequences (``"a\\nb"`` in a string, ``\\w+`` in a regex), so the
    transformation is only ever applied after the verbatim text failed to match —
    never unconditionally, which would corrupt those edits.
    """
    value = value.replace('\\"', '"')
    value = value.replace("\\'", "'")
    value = value.replace('\\n', '\n')
    value = value.replace('\\r', '\r')
    value = value.replace('\\t', '\t')
    value = value.replace('\\\\', '\\')
    return value


def _rstrip_lines(value: str) -> str:
    """Strip trailing whitespace from every line — the common cause of near-miss
    ``find`` failures (editor-added trailing spaces on either side)."""
    return "\n".join(line.rstrip() for line in value.split("\n"))


def _context_diff_window(before: str, after: str, context_lines: int = 4) -> tuple[str, str]:
    """Extract a focused window around the first difference in before/after.

    Returns a (before_window, after_window) pair trimmed to the changed region
    plus ``context_lines`` lines of surrounding context, so the frontend can
    render a meaningful diff without sending the entire file content.
    """
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)

    # Find the first and last differing line index.
    common_length = min(len(before_lines), len(after_lines))
    first_diff = 0
    for index in range(common_length):
        if before_lines[index] != after_lines[index]:
            first_diff = index
            break
    else:
        # No difference in the overlapping section — the difference is purely
        # one side being longer than the other.  Anchor at the common end.
        first_diff = common_length

    last_diff = first_diff
    for index in range(first_diff, common_length):
        if before_lines[index] != after_lines[index]:
            last_diff = index

    # Include added/removed lines at the end.
    if len(before_lines) != len(after_lines):
        last_diff = max(len(before_lines), len(after_lines)) - 1

    window_start = max(0, first_diff - context_lines)
    window_end = min(max(len(before_lines), len(after_lines)), last_diff + 1 + context_lines)

    before_window = "".join(before_lines[window_start:window_end]) if window_start < len(before_lines) else ""
    after_window = "".join(after_lines[window_start:window_end]) if window_start < len(after_lines) else ""
    return before_window, after_window


def _payload(code: str, **fields) -> str:
    """Build a JSON tool-result payload with the given ``code`` discriminator."""
    return json.dumps({"code": code, **fields})


def content_sha256(content: str) -> str:
    """Return the hex SHA-256 digest of the given content."""
    return hashlib.sha256(content.encode()).hexdigest()


def read_file(
    executor: LocationExecutor,
    base_directory: str,
    file_path: str,
    offset: int = 1,
    limit: int | None = None,
) -> str:
    """Read a file and return its lines in ``cat -n`` format.

    ``offset`` is the 1-indexed line to start reading from and ``limit`` caps the
    number of lines returned (``None`` reads to the end; a non-positive value falls
    back to the window-scaled default). Each returned line is prefixed with its
    right-aligned line number and a tab, exactly like ``cat -n``. Lines longer than
    the window-scaled line clip are cut; their numbers are reported in the
    ``long_lines_truncated`` field so the model knows their tails are missing (and
    must not be copied into an ``edit_file`` ``find``).
    """
    resolved_path = executor.resolve(base_directory, file_path)
    if not executor.exists(resolved_path):
        raise FileNotFoundError(f"Path does not exist: {resolved_path}")

    if executor.is_directory(resolved_path):
        raise IsADirectoryError(f"Path is a directory, not a file: {resolved_path}")

    tuning = active_tuning()
    line_clip = tuning.amount(Limit.MAXIMUM_LINE_CHARS)
    file_content = executor.read_text(resolved_path)
    file_lines = file_content.split("\n")
    start_line_index = max(1, offset)
    effective_limit = limit if (limit is None or limit > 0) else tuning.amount(Limit.READ_LINES)
    end_line_index = len(file_lines) if effective_limit is None else min(len(file_lines), start_line_index - 1 + effective_limit)
    selected_lines = file_lines[start_line_index - 1 : end_line_index]
    long_lines_truncated = [
        line_number
        for line_number, line in enumerate(selected_lines, start=start_line_index)
        if len(line) > line_clip
    ]
    rendered_output = "\n".join(
        f"{line_number:6d}\t{line[:line_clip]}"
        for line_number, line in enumerate(selected_lines, start=start_line_index)
    )
    is_truncated = start_line_index > 1 or end_line_index < len(file_lines)
    fields: dict[str, object] = {
        "path": resolved_path,
        "start_line": start_line_index,
        "end_line": end_line_index,
        "total_lines": len(file_lines),
        "truncated": is_truncated,
        "sha256": content_sha256(file_content),
        "content": rendered_output,
    }
    if long_lines_truncated:
        fields["long_lines_truncated"] = long_lines_truncated
    return _payload("read_completed", **fields)


def read_image_file(
    executor: LocationExecutor,
    base_directory: str,
    file_path: str,
    *,
    attach_pixels: bool,
) -> tuple[str, str | None]:
    """Read an image file, returning ``(payload_json, data_uri_or_None)``.

    The payload is the model/UI-facing tool result: structured metadata (mime
    type, byte size, pixel dimensions) with ``kind: "image"`` — never the base64
    itself, which would bloat every surface that stores the result. When
    ``attach_pixels`` is set and the file fits the inline ceiling, the second
    element carries a data URI the runtime attaches to the conversation as an
    image block so a vision model actually sees the pixels. Works against any
    location — the bytes come through the executor."""
    resolved_path = executor.resolve(base_directory, file_path)
    if not executor.exists(resolved_path):
        raise FileNotFoundError(f"Path does not exist: {resolved_path}")
    if executor.is_directory(resolved_path):
        raise IsADirectoryError(f"Path is a directory, not a file: {resolved_path}")

    raw = executor.read_bytes(resolved_path)
    mime_type = _IMAGE_MIME_BY_SUFFIX.get(Path(resolved_path).suffix.lower(), "application/octet-stream")
    width = height = None
    try:
        from PIL import Image
        import io as _io

        with Image.open(_io.BytesIO(raw)) as image:
            width, height = image.size
    except Exception:
        pass

    fields: dict[str, object] = {
        "path": resolved_path,
        "kind": "image",
        "mime_type": mime_type,
        "size_bytes": len(raw),
    }
    if width is not None:
        fields["width"] = width
        fields["height"] = height

    if not attach_pixels:
        fields["attached"] = False
        fields["message"] = (
            "Only this image's metadata is available for this read. Reason from the "
            "metadata, the file's role, and related sources you can read; do not "
            "tell the user you cannot see the image."
        )
        return _payload("read_completed", **fields), None
    if len(raw) > MAXIMUM_INLINE_IMAGE_BYTES:
        fields["attached"] = False
        fields["message"] = "The image is too large to attach inline; only its metadata is available."
        return _payload("read_completed", **fields), None

    fields["attached"] = True
    fields["message"] = "The image is attached to the conversation following this result."
    encoded = base64.b64encode(raw).decode("ascii")
    return _payload("read_completed", **fields), f"data:{mime_type};base64,{encoded}"


# Coordinate replacement engine


def _fuzzy_find_candidates(
    find: str,
    before: str,
    maximum_candidates: int = 5,
    threshold: float = 0.7,
) -> list[dict]:
    """Find near-matches of find within before using line-level indexing.

    Normalizes every line (rstrip), builds a hash lookup from line content
    to positions, then scores only windows where the first find line matches.
    This avoids the O(m * n) character-level sliding-window of SequenceMatcher.
    Falls back to SequenceMatcher on the first line only when the index
    produces no candidates.
    """
    from collections import defaultdict

    find_lines = find.split("\n")
    before_lines = before.split("\n")
    window_size = len(find_lines)

    # Reject empty or whitespace-only find texts
    if not find.strip() or not before_lines or window_size > len(before_lines):
        return []

    # Normalize once — rstrip covers the common trailing-whitespace mismatch
    normalized_find = [line.rstrip() for line in find_lines]
    normalized_before = [line.rstrip() for line in before_lines]

    # Content index: normalized line content -> list of line positions
    content_to_lines: dict[str, list[int]] = defaultdict(list)
    for position, line in enumerate(normalized_before):
        content_to_lines[line].append(position)

    def _score_window(start: int) -> float:
        """Average SequenceMatcher ratio across each line pair in the window.

        Per-line matching catches within-line whitespace differences
        (extra spaces, indentation shifts) that exact string comparison
        would miss, while being fast because individual lines are short.
        """
        from difflib import SequenceMatcher

        chunk = normalized_before[start : start + window_size]
        total_ratio = 0.0
        for find_line, before_line in zip(normalized_find, chunk):
            total_ratio += SequenceMatcher(None, find_line, before_line).ratio()
        return total_ratio / window_size

    def _candidate(start: int, ratio: float) -> dict:
        return {
            "text": "\n".join(before_lines[start : start + window_size]),
            "ratio": round(ratio, 4),
            "start_line": start + 1,
            "end_line": start + window_size,
        }

    candidates: list[dict] = []
    first_find_line = normalized_find[0]

    # Hash-lookup pass: O(N) index build, O(K) score (K = occurrences of first line)
    if first_find_line in content_to_lines:
        seen_starts: set[int] = set()
        for start in content_to_lines[first_find_line]:
            if start in seen_starts or start + window_size > len(before_lines):
                continue
            seen_starts.add(start)
            ratio = _score_window(start)
            if ratio >= threshold:
                candidates.append(_candidate(start, ratio))

    # Fallback pass when the hash-lookup pass found nothing and the first line
    # does not match literally but is still close. For single-line finds this
    # scans every line via SequenceMatcher; for multi-line finds it pre-filters
    # by first-line ratio then scores the full window.
    if not candidates:
        from difflib import SequenceMatcher

        seen_starts = set()
        if window_size == 1:
            for position, line in enumerate(normalized_before):
                ratio = SequenceMatcher(None, normalized_find[0], line).ratio()
                if ratio >= threshold:
                    candidates.append(_candidate(position, ratio))
                    if len(candidates) >= maximum_candidates:
                        break
        else:
            for start in range(len(before_lines) - window_size + 1):
                if start in seen_starts:
                    continue
                first_line_ratio = SequenceMatcher(
                    None, normalized_find[0], normalized_before[start]
                ).ratio()
                if first_line_ratio < 0.7:
                    continue
                seen_starts.add(start)
                ratio = _score_window(start)
                if ratio >= threshold:
                    candidates.append(_candidate(start, ratio))
                    if len(candidates) >= maximum_candidates:
                        break

    candidates.sort(key=lambda x: -x["ratio"])
    return candidates[:maximum_candidates]


def _edit_failure(code: str, message: str, **data: object) -> str:
    """A structured edit failure: one short human ``message`` plus the failure's facts as
    real structured fields (``candidates``, ``occurrences``, ``diagnostic``, ``path``) —
    never stitched into the message. The same payload serves the model and the UI, so there
    is no prose/data split to reconcile. ``status="error"`` marks the call failed for both."""
    return json.dumps({"code": code, "status": "error", "message": message.strip(), **data})


def _match_find_text(before: str, find: str, replace: str) -> tuple[str, str, str, int] | None:
    """Locate ``find`` in ``before``, trying progressively more forgiving variants.

    Order matters: the verbatim text first (so code that legitimately contains
    escape sequences is matched as written), then trailing-whitespace-normalized,
    then the escape-normalized fallback (for models that double-escaped their
    arguments), then both combined. Returns the effective
    ``(before, find, replace, occurrences)`` for the first variant that matches,
    or ``None`` when nothing does. When the escape-normalized ``find`` is the one
    that matched, ``replace`` is normalized the same way — a double-escaped call
    is double-escaped on both sides.
    """
    occurrences = before.count(find)
    if occurrences:
        return before, find, replace, occurrences

    normalized_before = _rstrip_lines(before)
    normalized_find = _rstrip_lines(find)
    occurrences = normalized_before.count(normalized_find)
    if occurrences:
        return normalized_before, normalized_find, replace, occurrences

    unescaped_find = _normalize_tool_escapes(find)
    if unescaped_find != find:
        unescaped_replace = _normalize_tool_escapes(replace)
        occurrences = before.count(unescaped_find)
        if occurrences:
            return before, unescaped_find, unescaped_replace, occurrences
        normalized_unescaped_find = _rstrip_lines(unescaped_find)
        occurrences = normalized_before.count(normalized_unescaped_find)
        if occurrences:
            return normalized_before, normalized_unescaped_find, unescaped_replace, occurrences

    return None


def edit_file(
    executor: LocationExecutor,
    base_directory: str,
    file_path: str,
    find: str,
    replace: str,
    *,
    expected_sha256: str | None,
    skip_validation: bool = False,
    replace_all: bool = False,
) -> str:
    """Edit a file by replacing ``find`` with ``replace``.

    ``find`` must occur verbatim in the file. Unless ``replace_all`` is set,
    it must be unique. The edit is staged in memory, validated against a
    language parser (AST or tree-sitter), and either committed or aborted with
    a diagnostic payload.

    When the caller provides a previously read SHA-256, it is checked in the
    isolation phase to reject stale edits.
    """
    resolved_path = executor.resolve(base_directory, file_path)
    if not executor.exists(resolved_path):
        raise FileNotFoundError(
            f"Cannot edit missing file: {resolved_path}. Use write_file to create new files."
        )
    if executor.is_directory(resolved_path):
        raise IsADirectoryError(f"Cannot edit directory: {resolved_path}")

    # Isolation — read and verify staleness when a prior read supplied a hash.
    before = executor.read_text(resolved_path)
    if expected_sha256 is not None and content_sha256(before) != expected_sha256:
        raise ValueError(
            f"{resolved_path} changed since it was last read. "
            "Call read_file again to get fresh content and line numbers."
        )

    if find == replace:
        raise ValueError("find and replace are identical; nothing to change.")

    matched = _match_find_text(before, find, replace)
    if matched is None:
        # Fuzzy match: look for near-misses to help the model debug
        near_misses = _fuzzy_find_candidates(find, before, threshold=0.7)
        if near_misses:
            return _edit_failure(
                "edit_find_near_miss",
                _VALIDATION_PROMPT_LOADER.load("find_near_miss", {}),
                path=resolved_path,
                candidates=near_misses[:3],
            )
        return _edit_failure(
            "edit_find_not_found",
            _VALIDATION_PROMPT_LOADER.load("find_not_found", {}),
            path=resolved_path,
        )
    before, find, replace, occurrences = matched

    # A non-unique `find` without `replace_all` is ambiguous, so fail rather
    # than silently editing the first occurrence.
    if occurrences > 1 and not replace_all:
        return _edit_failure(
            "edit_find_not_unique",
            _VALIDATION_PROMPT_LOADER.load("find_not_unique", {}),
            path=resolved_path,
            occurrences=occurrences,
        )

    # Execute — replace text
    after = before.replace(find, replace, -1 if replace_all else 1)

    # Verify — run language parser if registered
    if not skip_validation:
        suffix = Path(resolved_path).suffix.lower()
        parser = _LANGUAGE_PARSERS.get(suffix)
        if parser is not None:
            passed, message, line_number, column = parser(after)
            if not passed:
                diagnostic = {
                    "origin": "ast_parser",
                    "language": suffix.lstrip("."),
                    "line": line_number,
                    "column": column,
                    "message": message,
                    "context_snapshot": _context_snapshot(after.split("\n"), line_number or 1),
                }
                return _edit_failure(
                    "edit_failed_validation",
                    _VALIDATION_PROMPT_LOADER.load("validation_failure_recovery", {}),
                    path=resolved_path,
                    diagnostic=diagnostic,
                )

    # Commit — write through the executor
    executor.write_text(resolved_path, after)
    summary: dict[str, object] = {
        "code": "edit_completed",
        "path": resolved_path,
        "characters": len(after),
        "sha256": content_sha256(after),
    }
    # Include a focused diff window (changed lines + 4 lines of context) so the
    # frontend can render the change without sending the entire file contents.
    diff_before, diff_after = _context_diff_window(before, after, context_lines=4)
    summary["before"] = diff_before
    summary["after"] = diff_after
    return json.dumps(summary)


_LANGUAGE_PARSERS: dict[
    str, Callable[[str], tuple[bool, str | None, int | None, int | None]]
] = {}


def register_parser(suffix: str, parser_fn: Callable) -> None:
    """Register a validation parser for the given file suffix."""
    _LANGUAGE_PARSERS[suffix] = parser_fn


def _validate_python(
    content: str,
) -> tuple[bool, str | None, int | None, int | None]:
    """Validate Python content with the stdlib ``ast`` module."""
    try:
        ast.parse(content)
        return True, None, None, None
    except SyntaxError as exception:
        return False, exception.msg, exception.lineno, exception.offset


register_parser(".py", _validate_python)


def _context_snapshot(
    lines: list[str], line_number: int, radius: int = 3
) -> list[str]:
    """Extract a window of lines around ``line_number`` for diagnostic context."""
    start = max(0, line_number - 1 - radius)
    end = min(len(lines), line_number + radius)
    return lines[start:end]


# Tree-sitter validators (multi-language)

_TREE_SITTER_PARSERS: dict[str, Parser] = {}


def _validate_with_tree_sitter(source_bytes: bytes, language_id: str) -> tuple:
    """Validate content using a tree-sitter parser for the given language."""
    if language_id not in _TREE_SITTER_PARSERS:
        import tree_sitter_python as _py
        import tree_sitter_javascript as _js
        import tree_sitter_json as _jsn
        import tree_sitter_yaml as _yaml
        import tree_sitter_toml as _toml
        import tree_sitter_bash as _bash
        import tree_sitter_html as _html
        import tree_sitter_css as _css
        import tree_sitter_c as _c
        import tree_sitter_cpp as _cpp
        import tree_sitter_go as _go
        import tree_sitter_rust as _rust
        import tree_sitter_markdown as _md
        import tree_sitter_typescript as _ts
        from tree_sitter import Language, Parser

        _TABLE = {
            "python": Language(_py.language()),
            "javascript": Language(_js.language()),
            "json": Language(_jsn.language()),
            "yaml": Language(_yaml.language()),
            "toml": Language(_toml.language()),
            "bash": Language(_bash.language()),
            "html": Language(_html.language()),
            "css": Language(_css.language()),
            "c": Language(_c.language()),
            "cpp": Language(_cpp.language()),
            "go": Language(_go.language()),
            "rust": Language(_rust.language()),
            "markdown": Language(_md.language()),
            "typescript": Language(_ts.language_typescript()),
            "tsx": Language(_ts.language_tsx()),
        }
        for language_id, language in _TABLE.items():
            _TREE_SITTER_PARSERS[language_id] = Parser(language)

    parser = _TREE_SITTER_PARSERS.get(language_id)
    if parser is None:
        return True, None, None, None
    tree = parser.parse(source_bytes)
    root = tree.root_node
    if not root.has_error:
        return True, None, None, None
    cursor = root.walk()
    while True:
        current_node = cursor.node
        if current_node is None:
            return False, "syntax error", 1, 1
        if current_node.type == "ERROR":
            error_row, error_column = current_node.start_point
            error_node = current_node
            error_end = min(error_node.end_byte, len(source_bytes))
            error_text = source_bytes[error_node.start_byte : error_end].decode(
                "utf-8", errors="replace"
            )
            return False, f"syntax error near {error_text!r}", error_row + 1, error_column
        if not cursor.goto_first_child():
            while not cursor.goto_next_sibling():
                if not cursor.goto_parent():
                    return False, "syntax error", 1, 1


def _register_tree_sitter(extensions: list[str], language_id: str) -> None:
    """Register a tree-sitter-based validator for the given file extensions."""

    def validator(content: str) -> tuple:
        return _validate_with_tree_sitter(bytes(content, "utf-8"), language_id)

    for ext in extensions:
        register_parser(ext, validator)


_register_tree_sitter([".js", ".jsx", ".mjs", ".cjs"], "javascript")
_register_tree_sitter([".ts"], "typescript")
_register_tree_sitter([".tsx"], "tsx")
_register_tree_sitter([".json"], "json")
_register_tree_sitter([".yaml", ".yml"], "yaml")
_register_tree_sitter([".toml"], "toml")
_register_tree_sitter([".sh", ".bash", ".zsh"], "bash")
_register_tree_sitter([".html", ".htm"], "html")
_register_tree_sitter([".css", ".scss", ".less"], "css")
_register_tree_sitter([".c", ".h"], "c")
_register_tree_sitter([".cpp", ".hpp", ".cc", ".cxx"], "cpp")
_register_tree_sitter([".go"], "go")
_register_tree_sitter([".rs"], "rust")


def _edit_payload(
    code: str, path: str, *, created: bool, before: str, after: str
) -> str:
    """Build the tool result for write_file.

    ``before``/``after`` carry the full old and new file content for the UI's
    diff viewer; ``model_context`` is the lean summary the model actually sees
    (the file contents are redundant in the model's context — it already read
    the file and supplied the replacement — so they are stripped from the
    model-facing payload to avoid bloating it).
    """
    summary = {
        "code": code,
        "path": path,
        "created": created,
        "characters": len(after),
    }
    return json.dumps(
        {
            **summary,
            "before": before,
            "after": after,
            "model_context": summary,
        }
    )


def write_file(
    executor: LocationExecutor,
    base_directory: str,
    file_path: str,
    content: str,
    *,
    expected_sha256: str | None,
) -> str:
    """Write content to a file, overwriting it if it exists."""
    resolved_path = executor.resolve(base_directory, file_path)
    file_exists = executor.exists(resolved_path)
    if file_exists and executor.is_directory(resolved_path):
        raise IsADirectoryError(f"Path is a directory, not a file: {resolved_path}")
    before = executor.read_text(resolved_path) if file_exists else ""
    if file_exists and expected_sha256 is not None and content_sha256(before) != expected_sha256:
        raise ValueError(
            f"{resolved_path} changed since it was last read. "
            "Re-read the file before overwriting it."
        )
    executor.write_text(resolved_path, content)
    return _edit_payload(
        "write_completed", resolved_path, created=not file_exists, before=before, after=content
    )


async def fetch_url(url: str, fmt: str = "markdown", timeout: int = 30) -> str:
    """Fetch a URL and return its content in the requested format.

    The page is retrieved through a cascade of engines, each better at defeating
    JS-rendering and anti-bot walls than a raw request: Jina Reader first (free,
    handles most pages), Firecrawl next when a key is configured (full browser
    rendering for the hardest SPAs), and a plain HTTP GET as a last resort. The
    first engine to return non-thin content wins. A very large page is truncated
    inline, with the full content written to a temp file (``output_file``)."""
    from urllib.parse import urlparse

    fmt = (fmt or "markdown").lower()
    if fmt not in ("markdown", "text", "html"):
        fmt = "markdown"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("The URL must be a fully-formed http(s) URL.")

    content, engine = await _fetch_through_engines(url, fmt, timeout)

    inline_content, truncated = clip_to_tokens(content, active_tuning().amount(Limit.FETCH_TOKENS))
    fields: dict[str, object] = {"url": url, "format": fmt, "engine": engine, "truncated": truncated}
    if truncated:
        output_path = Path("/tmp") / f"{new_id('fetch')}.log"
        output_path.write_text(content)
        fields["output_file"] = str(output_path)
        fields["size"] = len(content)
    fields["content"] = inline_content
    return _payload("fetch_completed", **fields)


def _fetch_engines():
    """The fetch engines to try, in order. Firecrawl is only offered when a client
    is configured; Jina and the direct GET are always available."""
    yield ("jina", _fetch_via_jina)
    if _firecrawl_client is not None:
        yield ("firecrawl", _fetch_via_firecrawl)
    yield ("direct", _fetch_direct)


async def _fetch_through_engines(url: str, fmt: str, timeout: int) -> tuple[str, str]:
    """Walk the engine cascade, returning the first non-thin result. If every engine
    yields thin content, return the longest one; if all raise, raise a combined error."""
    best_content = ""
    best_engine = ""
    failures: list[str] = []
    for engine, fetcher in _fetch_engines():
        try:
            content = await fetcher(url, fmt, timeout)
        except Exception as error:  # noqa: BLE001 — any engine failure just falls through
            failures.append(f"{engine}: {error}")
            continue
        if len(content.strip()) >= MINIMUM_USEFUL_FETCH_CHARS:
            return content, engine
        if len(content) > len(best_content):
            best_content, best_engine = content, engine
    if best_content:
        return best_content, best_engine
    raise RuntimeError("Could not fetch the URL. " + "; ".join(failures))


# Jina Reader maps our format names onto its X-Return-Format header directly.
_JINA_RETURN_FORMAT = {"markdown": "markdown", "text": "text", "html": "html"}


async def _fetch_via_jina(url: str, fmt: str, timeout: int) -> str:
    """Fetch through Jina Reader (r.jina.ai) — returns clean content in the requested
    format. Works keyless; a configured key raises the rate limit."""
    import httpx

    headers = {"X-Return-Format": _JINA_RETURN_FORMAT[fmt]}
    if _jina_api_key:
        headers["Authorization"] = f"Bearer {_jina_api_key}"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(f"https://r.jina.ai/{url}", headers=headers)
        response.raise_for_status()
        return response.text


async def _fetch_via_firecrawl(url: str, fmt: str, timeout: int) -> str:
    """Fetch through Firecrawl's full-browser scrape (the configured client). Returns
    HTML for ``html``, otherwise its clean markdown."""
    scrape_format = "html" if fmt == "html" else "markdown"
    document = await _firecrawl_client.scrape(url, formats=[scrape_format], timeout=timeout * 1000)
    content = document.html if fmt == "html" else document.markdown
    return content or ""


async def _impersonated_get(url: str, timeout: int):
    """A GET that mimics a real Chrome — TLS/JA3 and HTTP/2 fingerprints, not just
    the User-Agent — so requests that a plain client gets flagged and blocked for
    still go through. Routes through the configured proxy when one is set. Returns the
    (fully-buffered) curl_cffi response, whose ``.text``/``.content`` stay readable
    after the session closes."""
    from curl_cffi import AsyncSession

    session_kwargs: dict[str, object] = {"impersonate": "chrome", "timeout": timeout}
    if _proxy_url:
        session_kwargs["proxies"] = {"http": _proxy_url, "https": _proxy_url}
    async with AsyncSession(**session_kwargs) as session:
        response = await session.get(url)
        response.raise_for_status()
        return response


async def _fetch_direct(url: str, fmt: str, timeout: int) -> str:
    """Last-resort direct fetch (browser-impersonated, no JS rendering) with local
    HTML→format conversion — the safety net when the scraping services are unset or
    unreachable, and the tier that defeats plain fingerprint-based blocks."""
    response = await _impersonated_get(url, timeout)
    body = response.text
    if fmt == "html":
        return body
    if fmt == "text":
        return _strip_html(body)
    return _markdownify(body)


async def download_file(executor: LocationExecutor, url: str, resolved_path: str, timeout: int = 120) -> str:
    """Download a URL's raw bytes to ``resolved_path`` using browser impersonation
    (and the configured proxy), then write them through the location executor so the
    file lands on the target location — local or remote. Returns the path, byte size,
    and content type."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("The URL must be a fully-formed http(s) URL.")

    response = await _impersonated_get(url, timeout)
    data = response.content
    content_type = response.headers.get("content-type", "")
    await asyncio.to_thread(executor.write_bytes, resolved_path, data)
    return _payload(
        "download_completed", url=url, path=resolved_path, bytes=len(data), content_type=content_type,
    )


def _strip_html(html: str) -> str:
    """Strip HTML tags and script/style content from a string."""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    return re.sub(r"\s+\n", "\n", text).strip()


__all__ = [
    "read_file",
    "read_image_file",
    "IMAGE_FILE_SUFFIXES",
    "content_sha256",
    "edit_file",
    "write_file",
    "fetch_url",
    "download_file",
    "set_jina_api_key",
    "set_firecrawl_client",
    "set_proxy_url",
]
