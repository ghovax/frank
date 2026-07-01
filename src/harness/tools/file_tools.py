"""Concrete implementations of the file/search/edit tools.

These are plain functions (synchronous for filesystem work, async for HTTP) that
the agent runtime dispatches to from ``_execute_tool``. They mirror opencode's
tool semantics but follow this harness's convention of returning results as
``json.dumps({...})`` payloads with a ``code`` discriminator — the same shape
``bash`` and ``web_search`` already use. Errors are raised so the runtime's
tool-call wrapper surfaces them as ERROR events to the model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from markdownify import markdownify as _markdownify

DEFAULT_READ_LINES = 2000
MAX_LINE_LENGTH = 2000
MAX_GREP_RESULTS = 500
MAX_GLOB_RESULTS = 1000
MAX_FETCH_CHARS = 200_000
MAX_TOOL_OUTPUT_CHARS = 1 << 16

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
    return line if len(line) <= MAX_LINE_LENGTH else line[:MAX_LINE_LENGTH]


def _payload(code: str, **fields) -> str:
    return json.dumps({"code": code, **fields})


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def read_lines(
    working_directory: str,
    file_path: str,
    start_line: int = 1,
    line_count: int | None = DEFAULT_READ_LINES,
    read_all: bool = False,
) -> str:
    """Read line-prefixed ranges from a file and return JSON."""
    path = _resolve(working_directory, file_path)
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")

    text = path.read_text(errors="replace")
    lines = text.split("\n")
    start = max(1, start_line)
    effective_count = None if read_all else (line_count if line_count and line_count > 0 else DEFAULT_READ_LINES)
    end = len(lines) if effective_count is None else min(len(lines), start - 1 + effective_count)
    selected = lines[start - 1:end]
    rendered = "\n".join(f"{idx}: {_truncate_line(line)}" for idx, line in enumerate(selected, start=start))
    truncated = start > 1 or end < len(lines)
    return _payload(
        "read_completed",
        path=str(path),
        start_line=start,
        end_line=end,
        total_lines=len(lines),
        truncated=truncated,
        sha256=content_sha256(text),
        content=rendered,
    )


def find_files(working_directory: str, pattern: str) -> str:
    """Match files by glob pattern, newest first. Returns JSON."""
    base = Path(working_directory) if working_directory else Path.cwd()
    if not base.exists():
        raise FileNotFoundError(f"Working directory does not exist: {base}")
    matches = [m for m in base.glob(pattern) if not m.is_dir()]
    matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    matches = matches[:MAX_GLOB_RESULTS]
    paths = [str(m) for m in matches]
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
                if len(results) >= MAX_GREP_RESULTS:
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
    matches = matches[:MAX_GREP_RESULTS]
    return _payload("search_completed", pattern=pattern, matches=matches, count=len(matches))


@dataclass
class PatchHunk:
    old_start: int = 1
    lines: list[tuple[str, str]] = field(default_factory=list)


def _parse_unified_diff(diff: str) -> list[PatchHunk]:
    lines = diff.splitlines()
    hunks: list[PatchHunk] = []
    index = 0
    header_pattern = re.compile(r"^@@ -(?P<old_start>\d+)(?:,\d+)? \+(?:\d+)(?:,\d+)? @@")

    while index < len(lines):
        line = lines[index]
        if not line.startswith("@@"):
            index += 1
            continue
        match = header_pattern.match(line)
        if match is None:
            raise ValueError(f"Invalid unified diff hunk header: {line}")
        hunk = PatchHunk(old_start=int(match.group("old_start")))
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if not line:
                raise ValueError("Unified diff hunk lines must start with ' ', '+', or '-'.")
            prefix = line[0]
            if prefix not in (" ", "+", "-"):
                raise ValueError("Unified diff hunk lines must start with ' ', '+', or '-'.")
            hunk.lines.append((prefix, line[1:]))
            index += 1
        if not hunk.lines:
            raise ValueError("Unified diff hunk cannot be empty.")
        hunks.append(hunk)

    if not hunks:
        raise ValueError("Patch must contain at least one unified diff hunk starting with '@@'.")
    return hunks


def _find_sequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    last = len(lines) - len(needle)
    for index in range(start, last + 1):
        if lines[index:index + len(needle)] == needle:
            return index
    preview = "\n".join(needle[:5])
    raise ValueError(f"Patch context did not match the current file content:\n{preview}")


def _apply_hunks(content: str, hunks: list[PatchHunk]) -> str:
    lines = content.split("\n")
    cursor = 0
    for hunk in hunks:
        old_lines = [text for prefix, text in hunk.lines if prefix in (" ", "-")]
        new_lines = [text for prefix, text in hunk.lines if prefix in (" ", "+")]
        preferred = max(0, hunk.old_start - 1)
        if not old_lines:
            index = min(preferred, len(lines))
        elif lines[preferred:preferred + len(old_lines)] == old_lines:
            index = preferred
        else:
            index = _find_sequence(lines, old_lines, cursor)
        lines = [*lines[:index], *new_lines, *lines[index + len(old_lines):]]
        cursor = index + len(new_lines)
    return "\n".join(lines)


def apply_patch(
    working_directory: str,
    file_path: str,
    diff: str,
    *,
    expected_sha256: str | None,
) -> str:
    path = _resolve(working_directory, file_path).expanduser().resolve(strict=False)
    if not path.exists():
        raise FileNotFoundError(f"Cannot patch missing file: {path}. Use write_file to create new files.")
    if path.is_dir():
        raise IsADirectoryError(f"Cannot patch directory: {path}")
    before = path.read_text(errors="replace")
    _validate_expected_hash(path, before, expected_sha256)
    after = _apply_hunks(before, _parse_unified_diff(diff))
    if after == before:
        raise ValueError(f"No changes to apply for {path}.")
    path.write_text(after)
    summary = {
        "code": "patch_completed",
        "path": str(path),
        "characters": len(after),
        "sha256": content_sha256(after),
    }
    return json.dumps({
        **summary,
        "before": before,
        "after": after,
        "model_context": summary,
    })


def _validate_expected_hash(path: Path, content: str, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        raise PermissionError(f"You must read {path} with read_lines before editing it.")
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
        raise PermissionError(f"You must read {path} with read_lines before overwriting it.")
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

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; agentic-harness)"})
        response.raise_for_status()
        body = response.text

    if fmt == "html":
        content = body
    elif fmt == "text":
        content = _strip_html(body)
    else:
        content = _markdownify(body)

    truncated = len(content) > MAX_FETCH_CHARS
    if truncated:
        content = content[:MAX_FETCH_CHARS]
    return _payload("fetch_completed", url=url, format=fmt, truncated=truncated, content=content)


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    return re.sub(r"\s+\n", "\n", text).strip()


__all__ = [
    "resolve_path",
    "read_lines",
    "find_files",
    "search_content",
    "content_sha256",
    "apply_patch",
    "write_file",
    "fetch_url",
]
