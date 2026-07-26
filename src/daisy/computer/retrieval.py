"""Semantic retrieval over a live surface — the engine behind ``find_one``/``find_many``.

The model states a goal in plain words; this ranks the elements (and, on the web, the page's
captured network exchanges) by relevance and hands back the best matches, each carrying its
native handle so the model can act on it. It is the same recipe ``semble`` uses for code —
static embeddings for semantic similarity, BM25 for lexical overlap, the two fused — but with a
**general** retrieval model rather than a code one, and with **no chunking**: the accessibility
tree already delivers the surface as discrete elements, so one element is one document.

Two deliberate choices, both settled empirically (see the plan ``screen-search-and-control``):

* **The document is the element's own words.** ``element_text`` joins the standardized
  accessibility fields — name, description, value, context — and nothing else. Role, state, and
  the native handle travel as structured metadata on the hit, never in the embedded text: adding
  them buys no retrieval signal and, in the pooled embedding space, collapses documents toward a
  common centroid (measured inter-document similarity 0.60 for a JSON dump vs 0.03 for the words).
* **Context is part of the words.** Dropping it costs ~13 points of top-1, because it is what
  tells twenty identical "Add to Cart" buttons apart.

The dense half is optional at runtime: when the embedding model cannot be loaded (no network to
fetch it, say), the index degrades to BM25 alone rather than failing — the model host being
unreachable must never take the tool down.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# The general, retrieval-tuned static model (model2vec). Not the code-specialized ``potion-code-16M``
# semble uses — a DOM/AX tree is natural-language UI labels, not code. Swappable in one place.
DENSE_MODEL = "minishlab/potion-retrieval-32M"

# Reciprocal-rank-fusion constant. Fuses the BM25 and dense rankings without having to reconcile
# their incomparable score scales: each document scores ``sum 1/(k + rank)`` over the rankings it
# appears in. The standard k=60 damps the tail so a strong #1 in one ranker still leads.
_RANK_FUSION_K = 60

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def element_text(name: str = "", description: str = "", value: Any = None, context: str = "") -> str:
    """The retrieval key for one element: its own words joined, empty fields dropped. The only
    text we embed or index — role/state/handle are structured metadata, kept out of this string."""
    value_text = value if isinstance(value, str) else ("" if value is None else str(value))
    return " ".join(part for part in (name, description, value_text, context) if part).strip()


@dataclass
class Document:
    """One indexed unit: a stable ``id`` (the native handle the model acts on), the ``text`` we
    rank against, and the ``payload`` returned verbatim to the model (role, state, full text)."""
    id: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]


class _BM25:
    """Okapi BM25. Pure Python, no dependency — this is the lexical half and the offline fallback.

    The two tuning constants are called ``k1`` and ``b`` in the literature; they are spelled out
    here as what they do, since neither letter means anything to a reader who has not just come
    from the paper."""

    def __init__(
        self,
        corpus: list[list[str]],
        saturation: float = 1.5,        # `k1`: how fast a repeated term stops adding score
        length_scaling: float = 0.75,   # `b`: how much a document's length discounts its terms
    ) -> None:
        self.corpus = corpus
        self.saturation = saturation
        self.length_scaling = length_scaling
        self.count = len(corpus)
        self.average_length = (sum(len(document) for document in corpus) / self.count) if self.count else 0.0
        document_frequency: dict[str, int] = {}
        for document in corpus:
            for term in set(document):
                document_frequency[term] = document_frequency.get(term, 0) + 1
        self.inverse_frequency = {
            term: math.log(1 + (self.count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }
        self._term_frequencies = [self._counts(document) for document in corpus]

    @staticmethod
    def _counts(document: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for term in document:
            counts[term] = counts.get(term, 0) + 1
        return counts

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * self.count
        for index, document in enumerate(self.corpus):
            length = len(document)
            if not length:
                continue
            frequencies = self._term_frequencies[index]
            total = 0.0
            for term in query:
                frequency = frequencies.get(term)
                if not frequency:
                    continue
                idf = self.inverse_frequency.get(term, 0.0)
                total += idf * (frequency * (self.saturation + 1)) / (
                    frequency
                    + self.saturation
                    * (1 - self.length_scaling + self.length_scaling * length / self.average_length)
                )
            out[index] = total
        return out


# The dense model is loaded once, lazily, and cached. ``False`` records a failed load so we do not
# retry the (possibly slow, possibly unreachable) fetch on every search — BM25 carries retrieval
# until the process restarts. ``None`` means "not yet attempted".
_dense: Any = None


def _dense_model() -> Any:
    global _dense
    if _dense is not None:
        return _dense or None
    try:
        from model2vec import StaticModel

        _dense = StaticModel.from_pretrained(DENSE_MODEL)
    except Exception:
        _dense = False
    return _dense or None


def _ranked_indices(scores: list[float]) -> list[int]:
    """Document indices ordered best-first by a score vector, ties broken by original order."""
    return sorted(range(len(scores)), key=lambda index: (-scores[index], index))


class Index:
    """A one-shot index over the current surface. Built fresh each search (a surface is small and
    mutates every action, so there is nothing to cache), it fuses a BM25 ranking with a dense
    ranking when the model is available, and returns hits carrying their native handle and payload."""

    def __init__(self, documents: list[Document]) -> None:
        self.documents = documents
        self._bm25 = _BM25([_tokens(document.text) for document in documents])
        self._dense_matrix: Any = None  # computed on first search if the model loads

    def _dense_scores(self, query: str) -> Optional[list[float]]:
        model = _dense_model()
        if model is None or not self.documents:
            return None
        import numpy as np

        if self._dense_matrix is None:
            vectors = np.asarray(model.encode([document.text for document in self.documents], show_progress_bar=False), dtype=np.float32)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            self._dense_matrix = vectors / np.clip(norms, 1e-9, None)
        query_vector = np.asarray(model.encode([query], show_progress_bar=False)[0], dtype=np.float32)
        query_vector = query_vector / max(float(np.linalg.norm(query_vector)), 1e-9)
        return (self._dense_matrix @ query_vector).tolist()

    def search(self, query: str, *, top_k: int, everything: bool = False) -> list[Hit]:
        """Rank the surface against ``query``. ``top_k`` best matches, or ``everything`` for the
        full ranking (bulk harvest). BM25 and — when the model is loaded — dense are fused by
        reciprocal rank fusion, so a hit strong in either ranker rises."""
        if not self.documents:
            return []
        lexical = self._reciprocal_rank_scores(self._bm25.scores(_tokens(query)))
        dense_scores = self._dense_scores(query)
        if dense_scores is None:
            fused = lexical
        else:
            semantic = self._reciprocal_rank_scores(dense_scores)
            fused = [lexical[index] + semantic[index] for index in range(len(self.documents))]
        order = _ranked_indices(fused)
        if not everything:
            order = order[:top_k]
        return [Hit(id=self.documents[index].id, score=fused[index], payload=self.documents[index].payload) for index in order]

    @staticmethod
    def _reciprocal_rank_scores(scores: list[float]) -> list[float]:
        """Convert a raw score vector into reciprocal-rank contributions, so unrelated score scales
        fuse cleanly. A document not surfaced by a ranker (score 0 here means no lexical overlap)
        simply contributes from its rank position like any other."""
        contribution = [0.0] * len(scores)
        for rank, index in enumerate(_ranked_indices(scores)):
            contribution[index] = 1.0 / (_RANK_FUSION_K + rank)
        return contribution
