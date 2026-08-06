"""Semantic code search, a thin wrapper over semble, which chunks with tree-sitter and embeds each chunk."""
from __future__ import annotations

import os
from typing import Any

# Built indexes by root, kept for the process's life; a rebuild is asked for explicitly with `reindex`.
_indexes: dict[str, Any] = {}


def _index_for(root: str, *, reindex: bool) -> Any:
    from semble import SembleIndex

    key = os.path.abspath(root)
    index = None if reindex else _indexes.get(key)
    if index is None:
        index = SembleIndex.from_path(key)
        _indexes[key] = index
    return index


def search_code(query: str, root: str = ".", *, top_k: int = 10, reindex: bool = False) -> dict:
    """Rank the code under ``root`` against ``query``, each hit with its file, line range and text."""
    if not query.strip():
        return {"ok": False, "error": "search_code needs a non-empty query."}
    try:
        index = _index_for(root, reindex=reindex)
    except Exception as error:
        return {"ok": False, "error": f"Code search is unavailable: {type(error).__name__}: {error}"}
    try:
        results = index.search(query, top_k=top_k)
    except Exception as error:
        return {"ok": False, "error": f"Code search failed: {type(error).__name__}: {error}"}
    matches = [
        {
            "file": result.chunk.file_path,
            "start_line": result.chunk.start_line,
            "end_line": result.chunk.end_line,
            "language": result.chunk.language,
            "snippet": result.chunk.content,
            "score": round(float(result.score), 4),
        }
        for result in results
    ]
    return {"ok": True, "query": query, "count": len(matches), "matches": matches}
