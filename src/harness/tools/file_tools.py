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


def read_file(working_directory: str, file_path: str, offset: int = 1, limit: int | None = None) -> str:
    """Read a file (line-prefixed) or list a directory. Returns JSON."""
    path = _resolve(working_directory, file_path)
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    if path.is_dir():
        entries = [f"{child.name}/" if child.is_dir() else child.name
                   for child in sorted(path.iterdir(), key=lambda p: p.name.lower())]
        return _payload("read_completed", path=str(path), is_directory=True, entries=entries)

    text = path.read_text(errors="replace")
    lines = text.split("\n")
    start = max(1, offset)
    end = len(lines)
    if limit is not None and limit > 0:
        end = min(end, start - 1 + limit)
    selected = lines[start - 1:end]
    rendered = "\n".join(f"{idx}: {_truncate_line(line)}" for idx, line in enumerate(selected, start=start))
    return _payload(
        "read_completed",
        path=str(path),
        is_directory=False,
        start_line=start,
        end_line=end,
        total_lines=len(lines),
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
    if shutil.which("rg"):
        try:
            matches = _grep_with_ripgrep(base, pattern, include)
        except (subprocess.SubprocessError, FileNotFoundError):
            matches = _grep_python(base, pattern, include)
    else:
        matches = _grep_python(base, pattern, include)
    matches = matches[:MAX_GREP_RESULTS]
    return _payload("search_completed", pattern=pattern, matches=matches, count=len(matches))


# --- edit: tolerant exact-string replacement (subset of opencode's cascade) ---


def _find_matches(content: str, old: str) -> list[str]:
    """Candidate exact spans in ``content`` that correspond to ``old``."""
    candidates: list[str] = []
    if old in content:
        candidates.append(old)

    original_lines = content.split("\n")
    search_lines = old.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
    for i in range(len(original_lines) - len(search_lines) + 1):
        window = original_lines[i:i + len(search_lines)]
        if len(window) == len(search_lines) and all(a.strip() == b.strip() for a, b in zip(window, search_lines)):
            block = "\n".join(window)
            if block not in candidates:
                candidates.append(block)

    norm_old = re.sub(r"\s+", " ", old).strip()
    if norm_old:
        for line in original_lines:
            if re.sub(r"\s+", " ", line).strip() == norm_old and line not in candidates:
                candidates.append(line)
    return candidates


def _is_disproportionate(matched: str, old: str) -> bool:
    matched_lines = matched.split("\n")
    old_lines = old.split("\n")
    if len(matched_lines) >= max(len(old_lines) + 3, len(old_lines) * 2):
        return True
    if len(old_lines) == 1:
        return False
    return len(matched.strip()) > max(len(old.strip()) + 500, len(old.strip()) * 4)


def apply_edit(content: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Return new_content. Raises on ambiguity or not-found."""
    if old_string == new_string:
        raise ValueError("No changes to apply: old_string and new_string are identical.")
    if old_string == "":
        return new_string

    matches = _find_matches(content, old_string)
    if not matches:
        raise ValueError(
            "Could not find old_string in the file. It must match exactly, including "
            "whitespace, indentation, and line endings.",
        )
    for matched in matches:
        if _is_disproportionate(matched, old_string):
            raise ValueError(
                "Refusing replacement because the matched span is much larger than "
                "old_string. Re-read the file and provide the full exact old_string.",
            )

    if replace_all:
        new_content = content
        for matched in matches:
            new_content = new_content.replace(matched, new_string)
        return new_content

    literal_unique = [m for m in matches if m == old_string and content.count(m) == 1]
    if literal_unique:
        return content.replace(literal_unique[0], new_string, 1)
    unique = [m for m in matches if content.count(m) == 1]
    if not unique:
        raise ValueError(
            "Found multiple matches for old_string. Provide more surrounding context, "
            "or set replace_all to true.",
        )
    return content.replace(unique[0], new_string, 1)


def edit_file(
    working_directory: str, file_path: str, old_string: str, new_string: str,
    replace_all: bool, *, has_been_read: bool,
) -> str:
    path = _resolve(working_directory, file_path)
    if old_string == "":
        if path.exists():
            raise ValueError(
                "old_string cannot be empty when editing an existing file. Provide the "
                "exact text to replace, or use write_file for a full-file replacement.",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_string)
        return _edit_payload("edit_completed", path, created=True, before="", after=new_string)

    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")
    if not has_been_read:
        raise PermissionError(f"You must read {path} with read_file before editing it.")

    content = path.read_text(errors="replace")
    new_content = apply_edit(content, old_string, new_string, replace_all)
    path.write_text(new_content)
    return _edit_payload("edit_completed", path, created=False, before=content, after=new_content)


def _edit_payload(code: str, path: Path, *, created: bool, before: str, after: str) -> str:
    """Result for edit_file/write_file. ``before``/``after`` carry the full old and
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
    working_directory: str, file_path: str, content: str, *, has_been_read: bool,
) -> str:
    path = _resolve(working_directory, file_path)
    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")
    if path.exists() and not has_been_read:
        raise PermissionError(f"You must read {path} with read_file before overwriting it.")
    before = path.read_text(errors="replace") if path.exists() and path.is_file() else ""
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
    "read_file",
    "find_files",
    "search_content",
    "apply_edit",
    "edit_file",
    "write_file",
    "fetch_url",
]
