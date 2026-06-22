from pathlib import Path

from langchain.tools import tool


@tool
def edit(path: str, old_string: str, new_string: str) -> str:
    """Edit a file by replacing the first occurrence of old_string with new_string.

    Args:
        path: Absolute path to the file.
        old_string: Text to search for (must exist exactly once in the file).
        new_string: Text to replace it with.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return f"File not found: {path}"
    if not resolved.is_file():
        return f"Not a file: {path}"

    content = resolved.read_text(encoding="utf-8")

    count = content.count(old_string)
    if count == 0:
        return f"old_string not found in file: {path}"
    if count > 1:
        return f"Found {count} matches for old_string in {path}. Provide more context."

    new_content = content.replace(old_string, new_string, 1)
    resolved.write_text(new_content, encoding="utf-8")
    return f"Edited {path}: replaced 1 occurrence"
