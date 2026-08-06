"""Semantic retrieval over a live surface — the engine behind ``find_one``/``find_many``."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class RetrievalPolicy:
    """Which models rank a screen, and when a query's spelling stops being evidence."""

    #: Ranks by meaning across languages, for a desktop whose labels are not all English.
    multilingual_rank_model: str = "minishlab/M2V_multilingual_output"

    #: A second embedding, ranked *alongside* the first rather than instead of it.
    english_rank_model: str = "minishlab/potion-base-32M"

    #: At or below this many words, a query is a label quoted off the screen and its spelling is the best evidence there is.
    lexical_gate_short_words: int = 3

    #: At or above this many words, a query is a description of a purpose: it shares no spelling with its target, so a character similarity ranks by coincidence — confidently, because a character cosine is never silent.
    lexical_gate_long_words: int = 7

    def weight_for(self, query: str) -> float:
        """How much the character signal is worth for this query, from how many words it has."""
        words = len(_tokens(query))
        if words <= self.lexical_gate_short_words:
            return 1.0
        if words >= self.lexical_gate_long_words:
            return 0.0
        span = self.lexical_gate_long_words - self.lexical_gate_short_words
        return 1.0 - (words - self.lexical_gate_short_words) / span


_policy = RetrievalPolicy()


def set_retrieval_policy(policy: RetrievalPolicy) -> None:
    """Bind the ranking policy for this process. Called from configuration load, like tuning."""
    global _policy
    _policy = policy


def active_retrieval_policy() -> RetrievalPolicy:
    return _policy


def retrieval_policy_from(section: object) -> RetrievalPolicy:
    """Build a policy from a loaded ``computer_control.retrieval`` section."""
    shipped = RetrievalPolicy()
    if section is None:
        return shipped
    return RetrievalPolicy(
        multilingual_rank_model=str(getattr(section, "multilingual_rank_model",
                                            shipped.multilingual_rank_model)),
        english_rank_model=str(getattr(section, "english_rank_model",
                                       shipped.english_rank_model)),
        lexical_gate_short_words=int(getattr(section, "lexical_gate_short_words",
                                             shipped.lexical_gate_short_words)),
        lexical_gate_long_words=int(getattr(section, "lexical_gate_long_words",
                                            shipped.lexical_gate_long_words)),
    )


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def lexical_weight(query: str) -> float:
    """How much the character signal is worth for this query, under the bound policy."""
    return _policy.weight_for(query)


# What each accessibility role is called in the language a person uses to ask for it.
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
    """Join an element's words: the kind of control, in words, then what it is called and says."""
    value_text = value if isinstance(value, str) else ("" if value is None else str(value))
    spoken = _ROLE_IN_WORDS.get(role, _ROLE_IN_WORDS.get(role.lower(), ""))
    return " ".join(part for part in (spoken, name, description, value_text, context) if part).strip()


# Path segments that appear on nearly every URL and therefore tell nothing apart.
_URL_NOISE_WORDS = frozenset({
    "www", "com", "org", "net", "http", "https", "html", "htm", "php", "aspx",
    "index", "wiki", "page", "en", "us", "docs",
})
_URL_SEPARATORS = re.compile(r"[^A-Za-z]+")


def url_in_words(url: str, *, keep_last: int = 4) -> str:
    """The readable words of a URL — ``/eng/house/share-house`` becomes ``house share house``."""
    words = [word for word in _URL_SEPARATORS.split(url or "")
             if len(word) > 2 and word.lower() not in _URL_NOISE_WORDS]
    return " ".join(words[-keep_last:])


def _without_repeated_words(text: str) -> str:
    """``text`` with later repeats of a word removed, keeping the first occurrence and the order."""
    kept: list[str] = []
    seen: set[str] = set()
    for word in text.split():
        folded = word.lower()
        if folded not in seen:
            seen.add(folded)
            kept.append(word)
    return " ".join(kept)


def text_or_fallback(text: str, fallback: str) -> str:
    """``text`` if it says anything, otherwise ``fallback`` — never both."""
    return text if text.strip() else fallback.strip()


def web_element_text(name: str = "", url: str = "", title: str = "", value: str = "") -> str:
    """The retrieval key for one element of a web page: what it is called, where it goes, and what it says it is for."""
    parts = (name, url_in_words(url), title)
    written = _without_repeated_words(" ".join(part for part in parts if part).strip())
    # `value` is a fallback, never an addition — see `text_or_fallback`.
    return text_or_fallback(written, value)


@dataclass
class Document:
    """One indexed unit: a stable ``id`` (the native handle the model acts on), the ``text`` we rank against, and the ``payload`` returned verbatim to the model (role, state, full text)."""
    id: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    parent: str = ""


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]


class WeakAnchor(Exception):
    """Raised when the element a caller anchored to could not itself be found confidently."""

    def __init__(self, anchor: str, margin: float) -> None:
        super().__init__(f"the anchor {anchor!r} matched nothing clearly (margin {margin:.3f})")
        self.anchor = anchor
        self.margin = margin


def _tree_path(identifier: str) -> tuple[str, ...]:
    """An element id as a path through the tree, or ``()`` when it is not one."""
    parts = identifier.split(".")
    return tuple(parts) if len(parts) > 1 and all(part.isdigit() for part in parts) else ()


def intent(query: str) -> Any:
    """A query as a unit vector, for asking whether a later one restates it."""
    model = _dense_model()
    if model is None:
        return None
    import numpy as np

    vector = np.asarray(model.encode([query], show_progress_bar=False)[0], dtype=np.float32)
    return vector / max(float(np.linalg.norm(vector)), 1e-9)


def _closeness(candidate: str, anchor: str) -> float:
    """How near two elements sit in the tree, from 0 (only the window in common) to 1 (siblings)."""
    first, second = _tree_path(candidate), _tree_path(anchor)
    if not first or not second:
        return 0.0
    shared = 0
    for left, right in zip(first, second):
        if left != right:
            break
        shared += 1
    return shared / max(len(first), len(second))


class _BM25:
    """Okapi BM25. Pure Python, no dependency — this is the lexical half and the offline fallback."""

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


class _Trigrams:
    """Character-trigram TF-IDF, and the reason it is here rather than an import."""

    __slots__ = ("vocabulary", "matrix", "weights", "count")

    def __init__(self, documents: list[str]) -> None:
        import numpy as np

        self.count = len(documents)
        self.vocabulary: dict[str, int] = {}
        rows: list[dict[int, int]] = []
        for text in documents:
            counts: dict[int, int] = {}
            for gram in _trigrams_of(text):
                position = self.vocabulary.setdefault(gram, len(self.vocabulary))
                counts[position] = counts.get(position, 0) + 1
            rows.append(counts)
        width = max(len(self.vocabulary), 1)
        matrix = np.zeros((self.count, width), dtype=np.float32)
        for index, counts in enumerate(rows):
            for position, count in counts.items():
                matrix[index, position] = count
        present = (matrix > 0).sum(axis=0)
        # Smoothed inverse document frequency, as the standard formulation has it: a trigram on every element says nothing, one on a single element says everything.
        self.weights = np.log((1 + self.count) / (1 + present)).astype(np.float32) + 1.0
        matrix *= self.weights
        self.matrix = matrix / np.clip(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9, None)

    def scores(self, query: str) -> Any:
        import numpy as np

        vector = np.zeros(self.matrix.shape[1], dtype=np.float32)
        for gram in _trigrams_of(query):
            position = self.vocabulary.get(gram)
            if position is not None:
                vector[position] += self.weights[position]
        norm = float(np.linalg.norm(vector))
        if norm < 1e-9:
            return np.zeros(self.count, dtype=np.float32)
        return self.matrix @ (vector / norm)


_TRIGRAM_PADDING = "  "
_WHITESPACE = re.compile(r"\s+")


def _trigrams_of(text: str) -> list[str]:
    """The character trigrams of a string, padded so its first and last letters count too."""
    folded = _TRIGRAM_PADDING + _WHITESPACE.sub(" ", text.lower().strip()) + _TRIGRAM_PADDING
    return [folded[position:position + 3] for position in range(len(folded) - 2)]


def _standardised(scores: Any) -> Any:
    """Scores centred and scaled by their own spread, so two signals can be added."""
    import numpy as np

    values = np.asarray(scores, dtype=np.float32)
    deviation = float(values.std())
    if deviation < 1e-9:
        return np.zeros_like(values)
    return (values - float(values.mean())) / deviation


# The dense models are loaded once, lazily, and cached by name.
_dense_models: dict[str, Any] = {}


def _model(name: str) -> Any:
    """One static model by name, or ``None`` if it is turned off or cannot be loaded."""
    if not name:
        return None
    if name not in _dense_models:
        try:
            from model2vec import StaticModel

            _dense_models[name] = StaticModel.from_pretrained(name)
        except Exception:
            _dense_models[name] = False
    return _dense_models[name] or None


def _dense_model() -> Any:
    """The primary model — the one whose cosine is comparable across queries."""
    return _model(_policy.multilingual_rank_model)


def _ranked_indices(scores: list[float]) -> list[int]:
    """Document indices ordered best-first by a score vector, ties broken by original order."""
    return sorted(range(len(scores)), key=lambda index: (-scores[index], index))


class Index:
    """A one-shot index over the current surface."""

    def __init__(self, documents: list[Document]) -> None:
        self.documents = documents
        self._bm25 = _BM25([_tokens(document.text) for document in documents])
        #: One embedded matrix per model, computed on first search if that model loads.
        self._dense_matrices: dict[str, Any] = {}
        self._trigrams: Optional[_Trigrams] = None
        #: Indices the ranker cannot represent — see :meth:`_unrepresentable_indices`.
        self._unreachable: set[int] = set()

    def _unrepresentable_indices(self, vectors: Any) -> set[int]:
        """Documents whose key encodes to the zero vector, and are therefore reachable by nothing."""
        import numpy as np

        norms = np.linalg.norm(vectors, axis=1)
        return {index for index in range(len(self.documents)) if float(norms[index]) < 1e-6}

    def _cosines(self, model_name: str, query: str) -> Optional[Any]:
        """One model's cosine between the query and every element, or ``None`` if it cannot load."""
        model = _model(model_name)
        if model is None or not self.documents:
            return None
        import numpy as np

        matrix = self._dense_matrices.get(model_name)
        if matrix is None:
            vectors = np.asarray(
                model.encode([document.text for document in self.documents],
                             show_progress_bar=False), dtype=np.float32)
            if model_name == _policy.multilingual_rank_model:
                self._unreachable = self._unrepresentable_indices(vectors)
                if self._unreachable:
                    logger.info("screen index: %d of %d keys encode to nothing and are "
                                "unsearchable", len(self._unreachable), len(self.documents))
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            matrix = vectors / np.clip(norms, 1e-9, None)
            self._dense_matrices[model_name] = matrix
        query_vector = np.asarray(model.encode([query], show_progress_bar=False)[0],
                                  dtype=np.float32)
        query_vector = query_vector / max(float(np.linalg.norm(query_vector)), 1e-9)
        return matrix @ query_vector

    def _dense_scores(self, query: str) -> Optional[list[float]]:
        """The primary model's cosine, as a plain list. What :meth:`anchored` finds an anchor by."""
        cosines = self._cosines(_policy.multilingual_rank_model, query)
        return None if cosines is None else cosines.tolist()

    def _ranking_scores(self, query: str) -> tuple[Optional[Any], Optional[Any]]:
        """What to rank by, and the plain cosine beside it."""
        primary = self._cosines(_policy.multilingual_rank_model, query)
        if primary is None:
            return None, None
        fused = _standardised(primary)
        second = self._cosines(_policy.english_rank_model, query)
        if second is not None:
            fused = fused + _standardised(second)
        weight = lexical_weight(query)
        if weight > 0.0:
            if self._trigrams is None:
                self._trigrams = _Trigrams([document.text for document in self.documents])
            fused = fused + weight * _standardised(self._trigrams.scores(query))
        return fused, primary

    def search(self, query: str, *, top_k: int, floor: float = 0.0) -> list[Hit]:
        """Rank the surface against ``query`` and return its ``top_k`` best matches, dropping any that score below ``floor``."""
        if not self.documents:
            return []
        ranking, cosines = self._ranking_scores(query)
        if ranking is not None:
            fused, unreachable = ranking.tolist(), self._unreachable
            # The floor is read off the *cosine*, never off the ranking score — see :meth:`_ranking_scores`.
            if floor > 0:
                admitted = {index for index in range(len(self.documents))
                            if float(cosines[index]) >= floor}
                unreachable = unreachable | (set(range(len(self.documents))) - admitted)
            cutoff = float("-inf")
        else:
            # Without the model, BM25 carries retrieval — and there the same elements are unreachable for the same reason by a different route: a private-use glyph yields no tokens at all, so it can never share one with a query.
            fused = self._bm25.scores(_tokens(query))
            unreachable = {index for index, document in enumerate(self.documents)
                           if not _tokens(document.text)}
            best = max(fused, default=0.0)
            cutoff = floor * best if floor > 0 and best > 0 else float("-inf")
        order = [index for index in _ranked_indices(fused)
                 if index not in unreachable and fused[index] >= cutoff]
        order = self._one_per_visible_thing(order)[:top_k]
        return [Hit(id=self.documents[index].id, score=fused[index], payload=self.documents[index].payload) for index in order]

    def _one_per_visible_thing(self, order: list[int]) -> list[int]:
        """Collapse ranked positions that are the same visible thing published more than once."""
        kept: list[int] = []
        seen: dict[tuple, int] = {}          # identity -> where its survivor sits in `kept`
        for index in order:
            payload = self.documents[index].payload
            name = str(payload.get("name") or "").strip()
            if not name:
                kept.append(index)
                continue
            identity = (name, str(payload.get("context") or ""), str(payload.get("url") or ""))
            position = seen.get(identity)
            if position is None:
                seen[identity] = len(kept)
                kept.append(index)
                continue
            incumbent = self.documents[kept[position]].payload
            if payload.get("clickable") and not incumbent.get("clickable"):
                kept[position] = index
        return kept

    def anchored(self, query: str, near: str, *, top_k: int, weight: float,
                 anchor_margin: float) -> list[Hit]:
        """Rank against ``query``, preferring elements that sit near whatever ``near`` matches."""
        if not self.documents:
            return []
        anchor_scores = self._dense_scores(near) or self._bm25.scores(_tokens(near))
        ranked = _ranked_indices(anchor_scores)
        best = ranked[0]
        top = anchor_scores[best]
        runner_up = anchor_scores[ranked[1]] if len(ranked) > 1 else 0.0
        margin = (top - runner_up) / top if top > 0 else 0.0
        if margin < anchor_margin:
            raise WeakAnchor(near, margin)
        # The anchor is found on the plain cosine above, because it is a *lookup* — one element, named as exactly as the caller can manage — and its margin test is calibrated against that.
        ranking, _cosines = self._ranking_scores(query)
        relevance = ranking.tolist() if ranking is not None else self._bm25.scores(_tokens(query))
        # Shifted to start at zero before scaling: a standardised score is centred on the mean, so roughly half of them are negative, and dividing by the maximum would leave the proximity term competing against a relevance term of the wrong sign.
        floor_value = min(relevance, default=0.0)
        relevance = [value - floor_value for value in relevance]
        ceiling = max(relevance) or 1.0
        anchor_id = self.documents[best].id
        combined = [
            relevance[index] / ceiling + weight * _closeness(document.id, anchor_id)
            for index, document in enumerate(self.documents)
        ]
        order = [index for index in _ranked_indices(combined)
                 if index not in self._unreachable and index != best][:top_k]
        return [Hit(id=self.documents[index].id, score=combined[index],
                    payload=self.documents[index].payload) for index in order]

