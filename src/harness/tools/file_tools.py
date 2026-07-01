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


def apply_line_replacement(content: str, start_line: int, end_line: int, new_lines: list[str]) -> str:
    """Replace an inclusive 1-indexed line range. ``end_line < start_line`` inserts."""
    if start_line < 1:
        raise ValueError("start_line must be 1 or greater.")
    if end_line < start_line - 1:
        raise ValueError("end_line must be at least start_line - 1.")
    if any("\n" in line or "\r" in line for line in new_lines):
        raise ValueError("new_lines entries must not contain newline characters; use one list item per line.")

    lines = content.split("\n")
    if start_line > len(lines) + 1:
        raise ValueError(f"start_line is past the end of the file. Last valid insertion line is {len(lines) + 1}.")
    if end_line > len(lines):
        raise ValueError(f"end_line is past the end of the file. Last existing line is {len(lines)}.")

    start_index = start_line - 1
    end_index = end_line
    return "\n".join([*lines[:start_index], *new_lines, *lines[end_index:]])


def replace_lines(
    working_directory: str, file_path: str, start_line: int, end_line: int, new_lines: list[str],
    *, expected_sha256: str | None,
) -> str:
    path = _resolve(working_directory, file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")
    if expected_sha256 is None:
        raise PermissionError(f"You must read {path} with read_lines before editing it.")

    content = path.read_text(errors="replace")
    if content_sha256(content) != expected_sha256:
        raise ValueError(f"{path} changed since it was last read. Re-read the file before editing it.")
    new_content = apply_line_replacement(content, start_line, end_line, new_lines)
    if new_content == content:
        raise ValueError("No changes to apply: replacement produced identical content.")
    path.write_text(new_content)
    return _edit_payload("replace_completed", path, created=False, before=content, after=new_content)


def _edit_payload(code: str, path: Path, *, created: bool, before: str, after: str) -> str:
    """Result for replace_lines/write_file. ``before``/``after`` carry the full old and
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
    "apply_line_replacement",
    "replace_lines",
    "write_file",
    "fetch_url",
]
