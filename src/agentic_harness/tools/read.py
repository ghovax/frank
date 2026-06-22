import os
from pathlib import Path

from langchain.tools import tool


@tool
def read(path: str, offset: int = 0, limit: int = 2000) -> str:
    """Read a file from the filesystem.

    Args:
        path: Absolute path to the file.
        offset: Line number to start reading from (1-indexed, default 0 = start).
        limit: Maximum number of lines to read (default 2000).
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return f"File not found: {path}"
    if not resolved.is_file():
        return f"Not a file: {path}"

    file_size = resolved.stat().st_size

    with open(resolved) as f:
        if offset > 0:
            for _ in range(offset - 1):
                next(f)
        lines = []
        for i, line in enumerate(f):
            if i >= limit:
                break
            lines.append(line)

    content = "".join(lines)
    info = f"File: {resolved} ({file_size} bytes, showing {len(lines)} lines from offset {offset})\n"
    return info + content
