"""Files parsed once and re-read only when they change, for the ones read on every turn."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, TypeVar

_Parsed = TypeVar("_Parsed")

#: path -> (stamp, parsed). The stamp is what the filesystem says, so an edit is picked up without a watcher.
_cache: dict[str, tuple[tuple[int, int], object]] = {}


def _stamp(path: Path) -> Optional[tuple[int, int]]:
    """Modification time and size together, since either alone misses an edit within the same second."""
    try:
        status = path.stat()
    except OSError:
        return None
    return (status.st_mtime_ns, status.st_size)


def parsed_file(path: Path, parse: Callable[[Path], _Parsed]) -> Optional[_Parsed]:
    """The parsed file, from memory unless it changed on disk; ``None`` when it is not there."""
    key = str(path)
    stamp = _stamp(path)
    if stamp is None:
        _cache.pop(key, None)
        return None
    held = _cache.get(key)
    if held is not None and held[0] == stamp:
        return held[1]  # type: ignore[return-value]
    parsed = parse(path)
    _cache[key] = (stamp, parsed)
    return parsed


def forget(path: Optional[Path] = None) -> None:
    """Drop one file, or all of them, for a caller that knows the content changed under it."""
    if path is None:
        _cache.clear()
    else:
        _cache.pop(str(path), None)
