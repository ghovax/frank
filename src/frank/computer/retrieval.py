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

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


# What each accessibility role is called in the language a person uses to ask for it. A query is
# almost always "the save button" or "the search field" — it names a kind of control as well as a
# label — so the kind has to be in the text that is embedded, in words. The raw role is not those
# words: including `AXButton` verbatim made retrieval *worse* than leaving the role out entirely,
# because it is a token an embedding model has never usefully seen.
_ROLE_IN_WORDS = {
    "AXButton": "button", "AXTextField": "text field", "AXTextArea": "text area",
    "AXStaticText": "text label", "AXRadioButton": "tab option", "AXCheckBox": "checkbox",
    "AXPopUpButton": "dropdown menu", "AXMenuButton": "menu button", "AXMenuItem": "menu item",
    "AXImage": "image", "AXLink": "link", "AXSlider": "slider", "AXIncrementor": "stepper",
    "AXRow": "row", "AXCell": "cell", "AXColumn": "column", "AXTabGroup": "tab bar",
    "AXOutline": "list", "AXTable": "table", "AXList": "list", "AXScrollBar": "scroll bar",
    "AXGroup": "group", "AXToolbar": "toolbar", "AXWindow": "window", "AXSheet": "dialog",
    "AXComboBox": "combo box", "AXProgressIndicator": "progress bar", "AXDisclosureTriangle": "disclosure arrow",
    "combobox": "combo box", "textbox": "text field", "searchbox": "search field",
    "link": "link", "button": "button", "checkbox": "checkbox", "radio": "radio button",
    "tab": "tab", "menuitem": "menu item", "heading": "heading", "listitem": "list item",
}


def element_text(name: str = "", description: str = "", value: Any = None, context: str = "",
                 role: str = "") -> str:
    """The retrieval key for one element: what it is, then what it says.

    The kind of control leads, in words, because that is how it is asked for. Measured across 143
    queries sampled from eight applications, naming the role this way moves top-1 from 119 to 132
    and MRR from 0.897 to 0.948 — several times the difference between any two embedding models
    tried, and unlike those, far larger than the sampling noise. An unrecognised role contributes
    nothing rather than its raw identifier, for the same reason `AXButton` is left out."""
    value_text = value if isinstance(value, str) else ("" if value is None else str(value))
    spoken = _ROLE_IN_WORDS.get(role, _ROLE_IN_WORDS.get(role.lower(), ""))
    return " ".join(part for part in (spoken, name, description, value_text, context) if part).strip()


@dataclass
class Document:
    """One indexed unit: a stable ``id`` (the native handle the model acts on), the ``text`` we
    rank against, and the ``payload`` returned verbatim to the model (role, state, full text).

    ``parent`` is the id of the element this one sits inside, empty at the top. The list stays
    flat, because the reader is a ranked search and a tree would neither rank nor truncate — but
    the shape of the screen is a fact about it, and a fact belongs in the data. It used to live
    only in the *spelling* of the ids, where a child's id was its parent's plus one step, so
    anything needing the hierarchy had to know that convention and take a string apart to use it.

    The native surface fills it in, because its elements are addressed by their path through the
    tree and it therefore knows. The browser leaves it empty: its elements are addressed by
    aria-ref, which says nothing about where an element sits, and it carries ``context`` — a
    trail of named ancestors — instead. That gap has never mattered, because a browser element
    has an accessible name and does not need its children to speak for it. A reader must treat
    an empty ``parent`` as "not known here", never as "top of the tree".
    """
    id: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    parent: str = ""


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
        full ranking (bulk harvest).

        The embedding model ranks. BM25 is the fallback for when it cannot load, and nothing
        more, because measurement said so: across 39 labelled queries on four applications,
        twelve fusion strategies were compared, and ranking by the model alone won on every
        family — including exact-match queries, where BM25 was supposed to be indispensable and
        scored 13/13 either way. Fusing the two was worse than either alone (MRR 0.685 against
        0.853), the signature of a fusion doing harm. Reciprocal rank fusion was the specific
        culprit: it credited every document by rank position, so one with no overlap at all
        still earned 65% of a perfect match's score, and 13 of 39 right answers fell out of the
        top three entirely."""
        if not self.documents:
            return []
        dense_scores = self._dense_scores(query)
        fused = dense_scores if dense_scores is not None else self._bm25.scores(_tokens(query))
        order = _ranked_indices(fused)
        if not everything:
            order = order[:top_k]
        return [Hit(id=self.documents[index].id, score=fused[index], payload=self.documents[index].payload) for index in order]

