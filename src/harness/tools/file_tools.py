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
MAXIMUM_GREP_RESULTS = 512
MAXIMUM_GLOB_RESULTS = 1024
MAXIMUM_FETCH_CHARS = 262_144
MAXIMUM_TOOL_OUTPUT_CHARS = 1 << 16

def _resolve(working_directory: str, file_path: str) -> Path:
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
    return line if len(line) <= MAXIMUM_LINE_LENGTH else line[:MAXIMUM_LINE_LENGTH]


def _payload(code: str, **fields) -> str:
    return json.dumps({"code": code, **fields})


def content_sha256(content: str) -> str:
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
    with its right-aligned line number and a tab, exactly like ``cat -n``."""
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
    selected_lines = file_lines[start_line_index - 1:end_line_index]
    rendered_output = "\n".join(f"{line_number:6d}\t{_truncate_line(line)}" for line_number, line in enumerate(selected_lines, start=start_line_index))
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
    command = ["rg", "--line-number", "--no-heading", "--color=never", "--max-count", "20"]
    if include:
        command += ["--glob", include]
    command += [pattern, str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    return (result.stdout or "").splitlines()


def _glob_to_regex(pattern: str) -> str:
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
    working_directory: str, pattern: str, include: str | None = None, path: str | None = None,
) -> str:
    """Search file contents by regex. Prefers ripgrep; falls back to a Python walk."""
    base = _resolve(working_directory, path) if path else (Path(working_directory) if working_directory else Path.cwd())
    if not base.exists():
        raise FileNotFoundError(f"Search path does not exist: {base}")
    home = Path.home().resolve(strict=False)
    resolved_base = base.expanduser().resolve(strict=False)
    if resolved_base == home:
        raise ValueError("Refusing to search the home directory. Narrow the search to a project folder or specific subdirectory.")
    if shutil.which("rg"):
        try:
            matches = _grep_with_ripgrep(base, pattern, include)
        except (subprocess.SubprocessError, FileNotFoundError):
            matches = _grep_python(base, pattern, include)
    else:
        matches = _grep_python(base, pattern, include)
    matches = matches[:MAXIMUM_GREP_RESULTS]
    return _payload("search_completed", pattern=pattern, matches=matches, count=len(matches))




# Coordinate-based staging engine

_VALID_OPERATION_TYPES = frozenset({
    "insert", "delete", "replace_range", "replace_text",
    "columnar_insert", "columnar_delete",
})

# Parser registry (extensible per language)

_LANGUAGE_PARSERS: dict[str, Callable[[str], tuple[bool, str | None, int | None, int | None]]] = {}


def register_parser(suffix: str, parser_fn: Callable) -> None:
    """Register a syntax validator for a file extension (with leading dot)."""
    _LANGUAGE_PARSERS[suffix] = parser_fn


def _validate_python(content: str) -> tuple[bool, str | None, int | None, int | None]:
    """Python syntax validation via stdlib ``ast.parse``.

    Returns ``(passed, message, line, column)``."""
    try:
        ast.parse(content)
        return True, None, None, None
    except SyntaxError as exception:
        return False, exception.msg, exception.lineno, exception.offset


register_parser(".py", _validate_python)


# Tree-sitter validators (multi-language)

_TREE_SITTER_PARSERS: dict[str, Parser] = {}


def _validate_with_tree_sitter(source_bytes: bytes, language_id: str, language_label: str) -> tuple:
    """Lazy-initialised tree-sitter validation.

    Returns ``(passed, message, line, column)`` with 1-indexed line numbers.
    """
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

        _LANGUAGE_TABLE = {
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
        for lang_id, lang_obj in _LANGUAGE_TABLE.items():
            _TREE_SITTER_PARSERS[lang_id] = Parser(lang_obj)

    parser = _TREE_SITTER_PARSERS.get(language_id)
    if parser is None:
        return True, None, None, None  # unknown language — skip

    tree = parser.parse(source_bytes)
    root = tree.root_node
    if not root.has_error:
        return True, None, None, None

    # Walk the tree for the first ERROR node
    cursor = root.walk()
    while True:
        if cursor.node.type == "ERROR":
            error_row, error_column = cursor.node.start_point
            return False, "syntax error", error_row + 1, error_column
        if not cursor.goto_first_child():
            while not cursor.goto_next_sibling():
                if not cursor.goto_parent():
                    return False, "syntax error", 1, 1


def _register_tree_sitter(extensions: list[str], language_id: str, language_label: str) -> None:
    """Register a tree-sitter validator for multiple file extensions."""
    def validator(content: str) -> tuple:
        return _validate_with_tree_sitter(bytes(content, "utf-8"), language_id, language_label)
    for ext in extensions:
        register_parser(ext, validator)


_register_tree_sitter([".js", ".jsx", ".mjs", ".cjs"], "javascript", "JavaScript")
_register_tree_sitter([".ts"], "typescript", "TypeScript")
_register_tree_sitter([".tsx"], "tsx", "TypeScript (TSX)")
_register_tree_sitter([".json"], "json", "JSON")
_register_tree_sitter([".yaml", ".yml"], "yaml", "YAML")
_register_tree_sitter([".toml"], "toml", "TOML")
_register_tree_sitter([".sh", ".bash", ".zsh"], "bash", "Bash")
_register_tree_sitter([".html", ".htm"], "html", "HTML")
_register_tree_sitter([".css", ".scss", ".less"], "css", "CSS")
_register_tree_sitter([".c", ".h"], "c", "C")
_register_tree_sitter([".cpp", ".hpp", ".cc", ".cxx"], "cpp", "C++")
_register_tree_sitter([".go"], "go", "Go")
_register_tree_sitter([".rs"], "rust", "Rust")



def _context_snapshot(lines: list[str], line_number: int, radius: int = 3) -> list[str]:
    """Extract a bounded subset of lines around ``line_number`` (1-indexed)."""
    start = max(0, line_number - 1 - radius)
    end = min(len(lines), line_number + radius)
    return lines[start:end]


# Schema validation

def _validate_operation_schema(operation: dict, index: int) -> None:
    """Validate that an operation dict has the required fields for its type.

    Raises ``ValueError`` on the first problem with a clear message."""
    operation_json = json.dumps(operation, indent=2)
    raw_type = operation.get("type")
    valid_types_str = json.dumps(sorted(_VALID_OPERATION_TYPES))

    def _error(field: str, expected: str, actual: str) -> ValueError:
        return ValueError(
            _VALIDATION_PROMPT_LOADER.load("operation_validation_error", {
                "index": str(index),
                "type": str(raw_type),
                "field": field,
                "expected": expected,
                "actual": actual,
                "operation_json": operation_json,
                "valid_types": valid_types_str,
            })
        )

    if not isinstance(raw_type, str) or raw_type not in _VALID_OPERATION_TYPES:
        raise _error("type", f"one of {valid_types_str}", repr(raw_type))

    for field_name in ("start_line", "end_line", "column", "length"):
        if field_name in operation and not isinstance(operation.get(field_name), int):
            raise _error(field_name, "a whole number", repr(operation.get(field_name)))

    start_line = operation.get("start_line")
    if not isinstance(start_line, int) or start_line < 1:
        raise _error("start_line", "a positive whole number (1 or greater)", repr(start_line))

    if raw_type in ("delete", "replace_range", "replace_text"):
        end_line = operation.get("end_line")
        if not isinstance(end_line, int) or end_line < start_line:
            raise _error("end_line", f"a whole number >= start_line ({start_line})", repr(end_line))

    if raw_type in ("insert", "replace_range"):
        text = operation.get("text")
        if not isinstance(text, str):
            raise _error("text", "a string", type(text).__name__)

    if raw_type in ("columnar_insert", "columnar_delete"):
        end_line = operation.get("end_line")
        if not isinstance(end_line, int) or end_line < start_line:
            raise _error("end_line", f"a whole number >= start_line ({start_line})", repr(end_line))
        column = operation.get("column")
        if not isinstance(column, int) or column < 0:
            raise _error("column", "a whole number (0 or greater)", repr(column))

    if raw_type == "columnar_insert":
        text = operation.get("text")
        if not isinstance(text, str):
            raise _error("text", "a string", type(text).__name__)

    if raw_type == "columnar_delete":
        length = operation.get("length")
        if not isinstance(length, int) or length < 0:
            raise _error("length", "a whole number (0 or greater)", repr(length))

    if raw_type == "replace_text":
        find_value = operation.get("find")
        if not isinstance(find_value, str):
            raise _error("find", "a string", type(find_value).__name__)
        replace_value = operation.get("replace")
        if not isinstance(replace_value, str):
            raise _error("replace", "a string", type(replace_value).__name__)


# Operation application (pure functions on list[str])

def _apply_insert(lines: list[str], operation: dict) -> list[str]:
    """Insert new lines before ``start_line`` (1-indexed)."""
    start_line = operation["start_line"]
    text = operation.get("text", "")
    new_lines = text.split("\n") if text else []
    insert_index = min(start_line - 1, len(lines))
    return lines[:insert_index] + new_lines + lines[insert_index:]


def _apply_delete(lines: list[str], operation: dict) -> list[str]:
    """Delete lines from ``start_line`` to ``end_line`` inclusive (1-indexed)."""
    start_line = operation["start_line"]
    end_line = operation["end_line"]
    if start_line > len(lines):
        raise ValueError(f"Delete start_line {start_line} exceeds file length {len(lines)}.")
    if end_line > len(lines):
        end_line = len(lines)
    return lines[: start_line - 1] + lines[end_line:]


def _apply_replace_range(lines: list[str], operation: dict) -> list[str]:
    """Atomic delete + insert at the same position."""
    start_line = operation["start_line"]
    end_line = operation["end_line"]
    text = operation.get("text", "")
    new_lines = text.split("\n") if text else []
    if start_line > len(lines):
        raise ValueError(f"Replace start_line {start_line} exceeds file length {len(lines)}.")
    effective_end = min(end_line, len(lines))
    return lines[: start_line - 1] + new_lines + lines[effective_end:]


def _apply_columnar_insert(lines: list[str], operation: dict) -> list[str]:
    """Insert ``text`` at ``column`` on every line in the range (inclusive)."""
    start_line = operation["start_line"]
    end_line = operation["end_line"]
    column = operation["column"]
    text = operation.get("text", "")
    result = list(lines)
    for line_index in range(start_line - 1, min(end_line, len(result))):
        line = result[line_index]
        if column > len(line):
            line = line + " " * (column - len(line))
        result[line_index] = line[:column] + text + line[column:]
    return result


def _apply_columnar_delete(lines: list[str], operation: dict) -> list[str]:
    """Delete ``length`` characters at ``column`` on every line in the range (inclusive)."""
    start_line = operation["start_line"]
    end_line = operation["end_line"]
    column = operation["column"]
    length = operation.get("length", 0)
    result = list(lines)
    for line_index in range(start_line - 1, min(end_line, len(result))):
        line = result[line_index]
        if column < len(line):
            result[line_index] = line[:column] + line[column + length:]
    return result


def _apply_replace_text(lines: list[str], operation: dict) -> list[str]:
    """Find and replace text within a bounded line range (inclusive)."""
    start_line = operation["start_line"]
    end_line = operation["end_line"]
    find_value = operation.get("find", "")
    replace_value = operation.get("replace", "")
    result = list(lines)
    for line_index in range(start_line - 1, min(end_line, len(result))):
        result[line_index] = result[line_index].replace(find_value, replace_value)
    return result


# Multi-operation executor

def apply_operations(lines: list[str], operations: list[dict]) -> list[str]:
    """Apply multiple operations to a line list, sorted in descending order.

    ``operations`` must already be schema-validated. The sort ensures that
    edits near the bottom of the file execute first, so line insertions and
    deletions do not shift the coordinates of pending operations above them.
    Columnar operations (which do not change line count) interleave naturally
    via the same sort."""
    sorted_operations = sorted(operations, key=lambda op: op.get("start_line", 0), reverse=True)
    result = list(lines)
    for operation in sorted_operations:
        op_type = operation["type"]
        try:
            if op_type == "insert":
                result = _apply_insert(result, operation)
            elif op_type == "delete":
                result = _apply_delete(result, operation)
            elif op_type == "replace_range":
                result = _apply_replace_range(result, operation)
            elif op_type == "columnar_insert":
                result = _apply_columnar_insert(result, operation)
            elif op_type == "columnar_delete":
                result = _apply_columnar_delete(result, operation)
            elif op_type == "replace_text":
                result = _apply_replace_text(result, operation)
        except ValueError as exception:
            raise ValueError(
                _VALIDATION_PROMPT_LOADER.load("operation_application_error", {
                    "operation_type": op_type,
                    "start_line": str(operation.get("start_line", "")),
                    "error": str(exception),
                })
            ) from exception
    return result


# Full transaction lifecycle

def execute_staged_edit(
    working_directory: str,
    file_path: str,
    operations_raw: list[dict],
    *,
    expected_sha256: str | None,
    skip_validation: bool = False,
) -> str:
    """Coordinate-based staging lifecycle for one file.

    Reads the file from disk, checks the SHA256 staleness guard,
    builds an in-memory line matrix, validates and applies the
    operations (sorted bottom-to-top), runs syntax validation
    against a registered language parser if available, and either
    commits the result to disk or aborts with a diagnostic payload.
    """
    resolved_path = _resolve(working_directory, file_path).expanduser().resolve(strict=False)
    if not resolved_path.exists():
        raise FileNotFoundError(
            f"Cannot edit missing file: {resolved_path}. Use write_file to create new files."
        )
    if resolved_path.is_dir():
        raise IsADirectoryError(f"Cannot edit directory: {resolved_path}")

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

    has_trailing_newline = before.endswith("\n")
    lines = before.split("\n")
    # Strip trailing empty line from final newline so the coordinate space
    # matches what read_file shows (no phantom empty line at the end).
    if lines and lines[-1] == "":
        lines = lines[:-1]

    if not isinstance(operations_raw, list):
        raise ValueError("'operations' must be a list of operation dicts.")
    for index, operation in enumerate(operations_raw):
        _validate_operation_schema(operation, index)

    mutated_lines = apply_operations(lines, operations_raw)
    after = "\n".join(mutated_lines)
    if has_trailing_newline:
        after += "\n"

    if not skip_validation:
        suffix = resolved_path.suffix.lower()
        parser = _LANGUAGE_PARSERS.get(suffix)
        if parser is not None:
            passed, message, line_number, column = parser(after)
            if not passed:
                # Validation failed — return diagnostic, disk untouched
                diagnostic = {
                    "origin": "ast_parser",
                    "language": suffix.lstrip("."),
                    "line": line_number,
                    "column": column,
                    "message": message,
                    "context_snapshot": _context_snapshot(mutated_lines, line_number or 1),
                }
                return json.dumps({
                    "code": "edit_failed_validation",
                    "path": str(resolved_path),
                    "diagnostic": diagnostic,
                    "suggested_action": _VALIDATION_PROMPT_LOADER.load("validation_failure_recovery", {}),
                })

    # Preserve the original line-ending convention (split already normalised).
    resolved_path.write_text(after)

    summary = {
        "code": "edit_completed",
        "path": str(resolved_path),
        "operations_applied": len(operations_raw),
        "characters": len(after),
        "sha256": content_sha256(after),
    }
    return json.dumps(summary)


# Coordinate-based edit_file entry point

def edit_file(
    working_directory: str,
    file_path: str,
    operations: list[dict],
    *,
    expected_sha256: str | None,
    skip_validation: bool = False,
) -> str:
    """Coordinate-based edit tool.

    ``operations`` is a list of operation dicts, each with a ``type``
    (``insert``, ``delete``, ``replace_range``, ``columnar_insert``,
    ``columnar_delete``) and the corresponding coordinate fields.

    The full staging lifecycle runs inside :func:`execute_staged_edit`:
    isolation, transaction, verification, then commit or abort.
    """
    return execute_staged_edit(
        working_directory,
        file_path,
        operations,
        expected_sha256=expected_sha256,
        skip_validation=skip_validation,
    )


def _validate_expected_hash(path: Path, content: str, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        raise PermissionError(f"You must read {path} with read_file before editing it.")
    if content_sha256(content) != expected_sha256:
        raise ValueError(f"{path} changed since it was last read. Re-read the file before editing it.")


def _edit_payload(code: str, path: Path, *, created: bool, before: str, after: str) -> str:
    """Result for write_file. ``before``/``after`` carry the full old and
    new file content for the UI's diff viewer; ``model_context`` is the lean summary
    the model actually sees (the file contents are redundant in the model's context
    — it already read the file and supplied the replacement — so they are stripped
    from the model-facing payload to avoid bloating it)."""
    summary = {
        "code": code,
        "path": str(path),
        "created": created,
        "characters": len(after),
    }
    return json.dumps({
        **summary,
        "before": before,
        "after": after,
        "model_context": summary,
    })


def write_file(
    working_directory: str, file_path: str, content: str, *, expected_sha256: str | None,
) -> str:
    path = _resolve(working_directory, file_path)
    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")
    if path.exists() and expected_sha256 is None:
        raise PermissionError(f"You must read {path} with read_file before overwriting it.")
    before = path.read_text(errors="replace") if path.exists() and path.is_file() else ""
    if path.exists() and path.is_file() and content_sha256(before) != expected_sha256:
        raise ValueError(f"{path} changed since it was last read. Re-read the file before overwriting it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return _edit_payload("write_completed", path, created=before == "", before=before, after=content)


async def fetch_url(url: str, fmt: str = "markdown", timeout: int = 30) -> str:
    import httpx

    fmt = (fmt or "markdown").lower()
    if fmt not in ("markdown", "text", "html"):
        fmt = "markdown"
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
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
    return _payload("fetch_completed", url=url, format=fmt, truncated=truncated, content=content)


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


def _is_cloudflare_challenge(status_code: int, headers: dict[str, str], body: str) -> bool:
    """Heuristic: does this response look like a Cloudflare anti-bot interstitial?"""
    # Cloudflare sets cf-mitigated: challenge on every Challenge Page response.
    # This is the authoritative signal per Cloudflare docs:
    # https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/
    return headers.get("cf-mitigated", "").lower() == "challenge"


async def _fetch_with_curl_cffi(url: str, timeout: int) -> str:
    """Fetch a URL using curl_cffi with full browser TLS/HTTP2 impersonation."""
    from curl_cffi import AsyncSession

    async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
        response = await session.get(url)
        response.raise_for_status()
        return response.text


def _strip_html(html: str) -> str:
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
