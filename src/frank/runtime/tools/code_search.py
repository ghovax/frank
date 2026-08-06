"""Semantic code search — the engine behind ``search_code``. A thin wrapper over
[semble](https://github.com/MinishLab/semble), which is purpose-built for this: it chunks a repo
with tree-sitter, embeds each chunk with a code-specialized static model, fuses that with BM25, and
returns the matching code with its file and line range. Unlike a screen ``find``, this genuinely is
code, so semble's code model and code chunking are exactly right — we use it directly rather than
reimplementing it.

The index reads straight from disk, so nothing here serialises the tree. Indexes are cached per
root for the process's life (building one is a few hundred milliseconds); a caller that has changed
files and wants freshness passes ``reindex``. When semble or its model cannot be loaded (no network
to fetch the model, say) the call returns a clear error rather than raising — a missing model must
not take the tool down.
"""
from __future__ import annotations

import os
from typing import Any

# root path to built index, kept for the process's life. semble reads from disk, so a rebuild is the
# only way to pick up edits; callers ask for that explicitly with ``reindex``.
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
    """Rank the code under ``root`` against ``query`` and return the best matching chunks, each with
    its file, line range, and text. ``reindex`` rebuilds the on-disk index first (use after edits)."""
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
