r"""Concrete implementations of the file/search/edit tools.

These are plain functions (synchronous for filesystem work, async for HTTP) that
the agent runtime dispatches to from ``_execute_tool``. They mirror opencode's
tool semantics but follow this harness's convention of returning results as
``json.dumps({...})`` payloads with a ``code`` discriminator — the same shape
``bash`` and ``web_search`` already use. Errors are raised so the runtime's
tool-call wrapper surfaces them as ERROR events to the model.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from tree_sitter import Parser

from markdownify import markdownify as _markdownify

from harness.core.configuration import PromptLoader as _PromptLoader

_VALIDATION_PROMPT_LOADER = _PromptLoader(Path(__file__).parent / "prompts")


DEFAULT_READ_LIMIT_LINES = 2048
MAXIMUM_LINE_LENGTH = 2048


def _normalize_tool_escapes(value: str) -> str:
    """Collapse JSON escape sequences that survived the tool-call parsing pipeline.

    When a model sees previously-serialised tool call arguments in its conversation
    history (e.g. ``\"`` rendered as ``\\\"`` inside a JSON string), it may copy the
    escaped form literally into its own tool call.  By the time the arguments reach
    this handler they have already been through ``json.loads`` once, which should
    have unescaped them — but some model providers double-encode, and some models
    re-escape when reading from history.  This normalises the common survivors so
    the plain-text string matching works reliably.
    """
    value = value.replace('\\"', '"')
    value = value.replace("\\'", "'")
    value = value.replace('\\n', '\n')
    value = value.replace('\\r', '\r')
    value = value.replace('\\t', '\t')
    value = value.replace('\\\\', '\\')
    return value


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


MAXIMUM_GREP_RESULTS = 512
MAXIMUM_GLOB_RESULTS = 1024
MAXIMUM_FETCH_CHARS = 262_144
MAXIMUM_TOOL_OUTPUT_CHARS = 1 << 16


def _resolve(working_directory: str, file_path: str) -> Path:
    """Resolve a possibly-relative path against the working directory."""
    candidate = Path(file_path)
    if not candidate.is_absolute():
        base = Path(working_directory) if working_directory else Path.cwd()
        candidate = base / file_path
    return candidate


def resolve_path(working_directory: str, file_path: str) -> Path:
    """Public wrapper around ``_resolve`` so the runtime can compute the same
    canonical path the implementations use (for read-before-edit tracking)."""
    return _resolve(working_directory, file_path)


def _truncate_line(line: str) -> str:
    """Truncate a line to the maximum allowed length for tool output."""
    return line if len(line) <= MAXIMUM_LINE_LENGTH else line[:MAXIMUM_LINE_LENGTH]


def _payload(code: str, **fields) -> str:
    """Build a JSON tool-result payload with the given ``code`` discriminator."""
    return json.dumps({"code": code, **fields})


def content_sha256(content: str) -> str:
    """Return the hex SHA-256 digest of the given content."""
    return hashlib.sha256(content.encode()).hexdigest()


def read_file(
    working_directory: str,
    file_path: str,
    offset: int = 1,
    limit: int | None = DEFAULT_READ_LIMIT_LINES,
) -> str:
    """Read a file and return its lines in ``cat -n`` format.

    ``offset`` is the 1-indexed line to start reading from and ``limit`` caps the
    number of lines returned (defaulting to 2048). Each returned line is prefixed
    with its right-aligned line number and a tab, exactly like ``cat -n``.
    """
    resolved_path = _resolve(working_directory, file_path)
    if not resolved_path.exists():
        raise FileNotFoundError(f"Path does not exist: {resolved_path}")

    if resolved_path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {resolved_path}")

    file_content = resolved_path.read_text(errors="replace")
    file_lines = file_content.split("\n")
    start_line_index = max(1, offset)
    effective_limit = limit if (limit is None or limit > 0) else DEFAULT_READ_LIMIT_LINES
    end_line_index = len(file_lines) if effective_limit is None else min(len(file_lines), start_line_index - 1 + effective_limit)
    selected_lines = file_lines[start_line_index - 1 : end_line_index]
    rendered_output = "\n".join(
        f"{line_number:6d}\t{_truncate_line(line)}"
        for line_number, line in enumerate(selected_lines, start=start_line_index)
    )
    is_truncated = start_line_index > 1 or end_line_index < len(file_lines)
    return _payload(
        "read_completed",
        path=str(resolved_path),
        start_line=start_line_index,
        end_line=end_line_index,
        total_lines=len(file_lines),
        truncated=is_truncated,
        sha256=content_sha256(file_content),
        content=rendered_output,
    )


def find_files(working_directory: str, pattern: str) -> str:
    """Match files by glob pattern, newest first. Returns JSON."""
    base = Path(working_directory) if working_directory else Path.cwd()
    if not base.exists():
        raise FileNotFoundError(f"Working directory does not exist: {base}")
    matches = [match for match in base.glob(pattern) if not match.is_dir()]
    matches.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    matches = matches[:MAXIMUM_GLOB_RESULTS]
    paths = [str(match) for match in matches]
    return _payload("find_completed", pattern=pattern, matches=paths, count=len(paths))


def _grep_with_ripgrep(path: Path, pattern: str, include: str | None) -> list[str]:
    """Run ripgrep and return matching lines."""
    command = ["rg", "--line-number", "--no-heading", "--color=never", "--max-count", "20"]
    if include:
        command += ["--glob", include]
    command += [pattern, str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    return (result.stdout or "").splitlines()


def _glob_to_regex(pattern: str) -> str:
    """Translate a simple glob pattern (``*`` and ``?`` only) to a regex."""
    translated = []
    for char in pattern:
        if char == "*":
            translated.append("[^/]*")
        elif char == "?":
            translated.append("[^/]")
        else:
            translated.append(re.escape(char))
    return "".join(translated)


def _grep_python(path: Path, pattern: str, include: str | None) -> list[str]:
    """Fallback grep using a pure-Python walk (used when ripgrep is unavailable)."""
    try:
        regex = re.compile(pattern)
    except re.error as exception:
        raise ValueError(f"Invalid regular expression: {exception}") from exception
    include_re = re.compile(_glob_to_regex(include)) if include else None
    results: list[str] = []
    for file in path.rglob("*"):
        if file.is_dir():
            continue
        if include_re is not None and not include_re.fullmatch(file.name):
            continue
        try:
            text = file.read_text(errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{file}:{line_no}:{line}")
                if len(results) >= MAXIMUM_GREP_RESULTS:
                    return results
    return results


def search_content(
    working_directory: str,
    pattern: str,
    include: str | None = None,
    path: str | None = None,
) -> str:
    """Search file contents by regex. Prefers ripgrep; falls back to a Python walk."""
    base = (
        _resolve(working_directory, path)
        if path
        else (Path(working_directory) if working_directory else Path.cwd())
    )
    if not base.exists():
        raise FileNotFoundError(f"Search path does not exist: {base}")
    home = Path.home().resolve(strict=False)
    resolved_base = base.expanduser().resolve(strict=False)
    if resolved_base == home:
        raise ValueError(
            "Refusing to search the home directory. "
            "Narrow the search to a project folder or specific subdirectory."
        )
    if shutil.which("rg"):
        try:
            matches = _grep_with_ripgrep(base, pattern, include)
        except (subprocess.SubprocessError, FileNotFoundError):
            matches = _grep_python(base, pattern, include)
    else:
        matches = _grep_python(base, pattern, include)
    matches = matches[:MAXIMUM_GREP_RESULTS]
    return _payload("search_completed", pattern=pattern, matches=matches, count=len(matches))


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


def edit_file(
    working_directory: str,
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
    language parser (AST or tree-sitter), and either committed to disk or
    aborted with a diagnostic payload.

    You must call ``read_file`` before editing: the SHA256 from that read is
    checked in the isolation phase to reject stale edits.
    """
    resolved_path = _resolve(working_directory, file_path).expanduser().resolve(strict=False)
    if not resolved_path.exists():
        raise FileNotFoundError(
            f"Cannot edit missing file: {resolved_path}. Use write_file to create new files."
        )
    if resolved_path.is_dir():
        raise IsADirectoryError(f"Cannot edit directory: {resolved_path}")

    # Normalise JSON escape artifacts that may have survived the tool-call
    # parsing pipeline — some model providers or history-round trips double-
    # encode quotes and newlines inside string arguments.
    find = _normalize_tool_escapes(find)
    replace = _normalize_tool_escapes(replace)

    # Isolation — read and verify staleness
    before = resolved_path.read_text(errors="replace")
    if expected_sha256 is None:
        raise PermissionError(
            f"You must read {resolved_path} with read_file before editing it."
        )
    if content_sha256(before) != expected_sha256:
        raise ValueError(
            f"{resolved_path} changed since it was last read. "
            "Call read_file again to get fresh content and line numbers."
        )

    if find == replace:
        raise ValueError("find and replace are identical; nothing to change.")

    occurrences = before.count(find)
    if occurrences == 0:
        # Fallback: strip trailing whitespace from both sides and retry.
        # This covers the common case where the model adds or omits trailing
        # spaces, or the file has editor-added trailing whitespace.
        normalized_find = "\n".join(line.rstrip() for line in find.split("\n"))
        normalized_before = "\n".join(line.rstrip() for line in before.split("\n"))
        occurrences = normalized_before.count(normalized_find)
        if occurrences == 0:
            # Fuzzy match: look for near-misses to help the model debug
            near_misses = _fuzzy_find_candidates(find, before, threshold=0.7)
            if near_misses:
                raise ValueError(
                    _VALIDATION_PROMPT_LOADER.load(
                        "find_near_miss",
                        {
                            "resolved_path": str(resolved_path),
                            "count": str(len(near_misses)),
                            "candidates": json.dumps(near_misses[:3]),
                        },
                    )
                )
            raise ValueError(
                _VALIDATION_PROMPT_LOADER.load(
                    "find_not_found", {"resolved_path": str(resolved_path)}
                )
            )
        # Normalization matched — use normalized versions for the replacement
        find = normalized_find
        before = normalized_before
        if occurrences > 1 and not replace_all:
            raise ValueError(
                _VALIDATION_PROMPT_LOADER.load(
                    "find_not_unique",
                    {"resolved_path": str(resolved_path), "occurrences": str(occurrences)},
                )
            )

    # Execute — replace text
    after = before.replace(find, replace, -1 if replace_all else 1)

    # Verify — run language parser if registered
    if not skip_validation:
        suffix = resolved_path.suffix.lower()
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
                return json.dumps(
                    {
                        "code": "edit_failed_validation",
                        "path": str(resolved_path),
                        "diagnostic": diagnostic,
                        "suggested_action": _VALIDATION_PROMPT_LOADER.load(
                            "validation_failure_recovery", {}
                        ),
                    }
                )

    # Commit — write to disk
    resolved_path.write_text(after)
    summary: dict[str, object] = {
        "code": "edit_completed",
        "path": str(resolved_path),
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

_TREE_SITTER_PARSERS: dict[str, "Parser"] = {}


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
        if cursor.node.type == "ERROR":
            error_row, error_column = cursor.node.start_point
            error_node = cursor.node
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


def _validate_expected_hash(
    path: Path, content: str, expected_sha256: str | None
) -> None:
    """Assert that ``content`` matches the expected SHA-256 hash, raising if stale."""
    if expected_sha256 is None:
        raise PermissionError(
            f"You must read {path} with read_file before editing it."
        )
    if content_sha256(content) != expected_sha256:
        raise ValueError(
            f"{path} changed since it was last read. "
            "Re-read the file before editing it."
        )


def _edit_payload(
    code: str, path: Path, *, created: bool, before: str, after: str
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
        "path": str(path),
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
    working_directory: str,
    file_path: str,
    content: str,
    *,
    expected_sha256: str | None,
) -> str:
    """Write content to a file, overwriting it if it exists. Staleness-checked."""
    path = _resolve(working_directory, file_path)
    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")
    if path.exists() and expected_sha256 is None:
        raise PermissionError(
            f"You must read {path} with read_file before overwriting it."
        )
    before = path.read_text(errors="replace") if path.exists() and path.is_file() else ""
    if path.exists() and path.is_file() and content_sha256(before) != expected_sha256:
        raise ValueError(
            f"{path} changed since it was last read. "
            "Re-read the file before overwriting it."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return _edit_payload(
        "write_completed", path, created=before == "", before=before, after=content
    )


async def fetch_url(url: str, fmt: str = "markdown", timeout: int = 30) -> str:
    """Fetch a URL and return its content in the requested format."""
    import httpx

    fmt = (fmt or "markdown").lower()
    if fmt not in ("markdown", "text", "html"):
        fmt = "markdown"
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("The URL must be a fully-formed valid https URL.")

    body = await _fetch_with_cloudflare_bypass(url, timeout)

    if fmt == "html":
        content = body
    elif fmt == "text":
        content = _strip_html(body)
    else:
        content = _markdownify(body)

    truncated = len(content) > MAXIMUM_FETCH_CHARS
    if truncated:
        content = content[:MAXIMUM_FETCH_CHARS]
    return _payload(
        "fetch_completed", url=url, format=fmt, truncated=truncated, content=content
    )


async def _fetch_with_cloudflare_bypass(url: str, timeout: int) -> str:
    """Fetch a URL, transparently retrying with a browser-impersonated client
    if the initial request is blocked by a Cloudflare challenge."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        body = response.text

    # If this looks like a Cloudflare challenge, retry with curl_cffi
    if _is_cloudflare_challenge(response.status_code, dict(response.headers), body):
        try:
            body = await _fetch_with_curl_cffi(url, timeout)
        except ImportError:
            pass  # curl_cffi not installed — honour the original challenge response
        else:
            return body

    # Normal path: raise on HTTP errors, return body on success
    response.raise_for_status()
    return body


def _is_cloudflare_challenge(
    status_code: int, headers: dict[str, str], body: str
) -> bool:
    """Heuristic: does this response look like a Cloudflare anti-bot interstitial?

    Cloudflare sets cf-mitigated: challenge on every Challenge Page response.
    This is the authoritative signal per Cloudflare docs:
    https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/
    """
    return headers.get("cf-mitigated", "").lower() == "challenge"


async def _fetch_with_curl_cffi(url: str, timeout: int) -> str:
    """Fetch a URL using curl_cffi with full browser TLS/HTTP2 impersonation."""
    from curl_cffi import AsyncSession

    async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
        response = await session.get(url)
        response.raise_for_status()
        return response.text


def _strip_html(html: str) -> str:
    """Strip HTML tags and script/style content from a string."""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    return re.sub(r"\s+\n", "\n", text).strip()


__all__ = [
    "resolve_path",
    "read_file",
    "find_files",
    "search_content",
    "content_sha256",
    "edit_file",
    "write_file",
    "fetch_url",
]
