r"""Concrete implementations of the file/search/edit tools.

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
MAXIMUM_LINE_LENGTH = 2000
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
    limit: int | None = DEFAULT_READ_LINES,
) -> str:
    """Read a file and return its lines in ``cat -n`` format.

    ``offset`` is the 1-indexed line to start reading from and ``limit`` caps the
    number of lines returned (defaulting to 2000). Each returned line is prefixed
    with its right-aligned line number and a tab, exactly like ``cat -n``."""
    path = _resolve(working_directory, file_path)
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")

    text = path.read_text(errors="replace")
    lines = text.split("\n")
    start = max(1, offset)
    effective_limit = limit if (limit is None or limit > 0) else DEFAULT_READ_LINES
    end = len(lines) if effective_limit is None else min(len(lines), start - 1 + effective_limit)
    selected = lines[start - 1:end]
    rendered = "\n".join(f"{idx:6d}\t{_truncate_line(line)}" for idx, line in enumerate(selected, start=start))
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
    matches = [match for match in base.glob(pattern) if not match.is_dir()]
    matches.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    matches = matches[:MAXIMUM_GLOB_RESULTS]
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




def edit_file(
    working_directory: str,
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    *,
    expected_sha256: str | None,
) -> str:
    """Replace an exact substring in one existing file.

    Mirrors the semantics of a string-replacement edit tool: ``old_string`` must
    occur verbatim in the file and, unless ``replace_all`` is set, must be unique.
    The file must have been read first (its hash is checked against
    ``expected_sha256``) so stale edits are rejected."""
    path = _resolve(working_directory, file_path).expanduser().resolve(strict=False)
    if not path.exists():
        raise FileNotFoundError(f"Cannot edit missing file: {path}. Use write_file to create new files.")
    if path.is_dir():
        raise IsADirectoryError(f"Cannot edit directory: {path}")
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical; nothing to change.")

    before = path.read_text(errors="replace")
    _validate_expected_hash(path, before, expected_sha256)

    occurrences = before.count(old_string)
    if occurrences == 0:
        raise ValueError(
            f"old_string not found in {path}. Copy it character-for-character from the read_file output "
            "(whitespace and quote style must match exactly)."
        )
    if occurrences > 1 and not replace_all:
        raise ValueError(
            f"old_string is not unique in {path}: {occurrences} matches. Add surrounding context to make it "
            "unique, or set replace_all=true to replace every occurrence."
        )

    after = before.replace(old_string, new_string) if replace_all else before.replace(old_string, new_string, 1)
    replacements = occurrences if replace_all else 1
    path.write_text(after)

    summary = {
        "code": "edit_completed",
        "path": str(path),
        "characters": len(after),
        "replacements": replacements,
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

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        body = response.text

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
