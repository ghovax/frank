"""Semantic retrieval over a live surface — the engine behind ``find_one``/``find_many``.

The model states a goal in plain words; this ranks the elements (and, on the web, the page's
captured network exchanges) by relevance and hands back the best matches, each carrying its
native handle so the model can act on it. It starts from the recipe ``semble`` uses for code, with
a **general** retrieval model rather than a code one and with **no chunking** — the accessibility
tree already delivers the surface as discrete elements, so one element is one document.

It departs from that recipe in one way, and the departure is measured: **the two rankers are not
fused.** Static embeddings rank; BM25 exists only as a fallback for when the embedding model
cannot be loaded. :meth:`Index.search` records why — fusing them was worse than either alone.
This paragraph used to say "the two fused", describing a design that had already been measured
and removed, which is the same way the claim that ``context`` belonged in the key outlived the
evidence against it.

Two deliberate choices, both settled empirically (see the plan ``screen-search-and-control``):

* **The document is the element's own words**, and only the ones that discriminate. Everything
  else about an element — its state, its native handle, its position — travels as structured
  metadata on the hit rather than in the embedded text, because in a pooled embedding space
  extra text pulls every document toward a common centroid (measured inter-document similarity
  0.60 for a JSON dump against 0.03 for the words).
* **Context belongs in the payload, not in the key.** This reversed an earlier finding, and the
  reversal is the largest single result in ``the-input-is-the-ceiling``: writing the nearest
  labelling ancestor into the key makes every child of a section look alike to a cosine, which
  is the opposite of the disambiguation it was added for. Measured on 797 queries drawn from
  eight live websites, dropping it moves top-1 by 14 to 19 points depending on the base it is
  measured against, every interval well clear of zero. The
  earlier "+13 points for keeping it" came from a query set that had been built by joining the
  element's own name to its role, which fed the key back into the query.

The two surfaces build their keys differently and deliberately. The browser has a link's
destination and a tooltip to work with, so :func:`web_element_text` uses them; a native window has
neither, and ranks on the element's own words followed by the kind of control the application
says it is. Both fall back to the element's ``value`` when that leaves nothing to rank — see
:func:`text_or_fallback`, which is what makes the two thirds of a native window that carry no name
reachable at all.

The dense half is optional at runtime: when the embedding model cannot be loaded (no network to
fetch it, say), the index degrades to BM25 alone rather than failing — the model host being
unreachable must never take the tool down.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The general, retrieval-tuned static model (model2vec), swappable in one place.
#
# Not the code-specialized ``potion-code-16M`` semble uses, and this is now measured rather than
# assumed: six static models were compared on both surfaces, and no model — code-trained or
# otherwise — was separably better than this one on the shipped key. Indexing markup instead of an
# element's words was measured too, and loses by 17% even when only elements that *have* markup are
# counted. A page's controls are natural-language labels; the code around them is shared
# boilerplate that collapses the embedding space rather than distinguishing anything in it.
DENSE_MODEL = "minishlab/M2V_multilingual_output"

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
    """Join an element's words: the kind of control, in words, then what it is called and says.

    Still used for the text the model *reads*, where more context is a kindness rather than a
    cost. It is no longer what either surface *embeds* — see :func:`web_element_text`, and
    :mod:`frank.computer.engine`, which builds its key from the element's own words and the kind
    of control the application says it is.

    This docstring used to claim that leading with the role moved top-1 from 119 to 132 across
    143 queries. That measurement was real but its queries were not: each was built by joining an
    element's own name to its role, so a key containing the role was being scored against a query
    containing the role. On queries drawn only from what an application actually wrote, ranking on
    the name alone beat name-with-role-and-value by 2.5% (95% interval [0.8%, 4.2%]) across ten
    live applications. The number was never wrong; the thing it measured was.

    That second measurement had a caveat of its own, and the caveat has since been paid: those
    queries never named a kind of control either, so they could no more reward a role than the
    first set could fail to. 137 queries logged from live sessions settle it — **56% name a kind**
    — and :mod:`frank.computer.engine` now appends ``role_description`` to the native key on the
    strength of it. What survives here is the narrower claim this function still makes true: an
    unrecognised *role identifier* contributes nothing rather than its raw spelling, because
    ``AXButton`` is a token no embedding has usefully seen. The application's own prose for the
    kind is a different thing from the platform's identifier for it."""
    value_text = value if isinstance(value, str) else ("" if value is None else str(value))
    spoken = _ROLE_IN_WORDS.get(role, _ROLE_IN_WORDS.get(role.lower(), ""))
    return " ".join(part for part in (spoken, name, description, value_text, context) if part).strip()


# Path segments that appear on nearly every URL and therefore tell nothing apart. Kept small on
# purpose: a stop list that grows starts removing words a page actually meant.
_URL_NOISE_WORDS = frozenset({
    "www", "com", "org", "net", "http", "https", "html", "htm", "php", "aspx",
    "index", "wiki", "page", "en", "us", "docs",
})
_URL_SEPARATORS = re.compile(r"[^A-Za-z]+")


def url_in_words(url: str, *, keep_last: int = 4) -> str:
    """The readable words of a URL — ``/eng/house/share-house`` becomes ``house share house``.

    Only the tail is kept, because a path narrows left to right and the last segments are the
    ones that name the destination. Short and boilerplate segments are dropped so that the words
    which survive are the ones a person would use.

    This is the single largest source of retrieval signal the browser surface has beyond an
    element's own label: on 212 queries whose wording shared no word at all with the visible
    label, including these words moved top-1 from 8% to 39%."""
    words = [word for word in _URL_SEPARATORS.split(url or "")
             if len(word) > 2 and word.lower() not in _URL_NOISE_WORDS]
    return " ".join(words[-keep_last:])


def _without_repeated_words(text: str) -> str:
    """``text`` with later repeats of a word removed, keeping the first occurrence and the order.

    A link commonly names itself the same way three times over — visible text, URL slug, and
    tooltip — and a repeated word is weight in the embedding without being information."""
    kept: list[str] = []
    seen: set[str] = set()
    for word in text.split():
        folded = word.lower()
        if folded not in seen:
            seen.add(folded)
            kept.append(word)
    return " ".join(kept)


def text_or_fallback(text: str, fallback: str) -> str:
    """``text`` if it says anything, otherwise ``fallback`` — never both.

    The distinction between a fallback and an addition is the whole of it, and it took two
    measurements to see. Appending an element's ``value`` to its name costs accuracy, which is why
    ``value`` was removed from both keys. But an element with *no* name has an empty key, and an
    empty key cannot be retrieved by any query at all: it is not ranked badly, it is unreachable.

    Two thirds of recorded native elements are in that state — 999 of 1,455, of which 927 do carry
    a value, mostly static text whose words live in ``value`` rather than in ``name``. Falling
    back moves those from 0% to 89.6% top-1 (95% interval [+85.9%, +92.9%]) and costs 4.2% on the
    elements that already had a name. On the browser surface the same trade is 0% to 67.0% for
    0.8%. Making two thirds of a surface reachable is worth a few points on the third that already
    was."""
    return text if text.strip() else fallback.strip()


def web_element_text(name: str = "", url: str = "", title: str = "", value: str = "") -> str:
    """The retrieval key for one element of a web page: what it is called, where it goes, and
    what it says it is for.

    Three parts. The **name** leads because it is what a query usually says. The **URL words**
    follow, which is the change that separated this key from every alternative: on queries whose
    wording shares no word with the visible label, they move top-1 from 8% to 39%. The **title**
    is the page's own prose description, present on about a quarter of elements and the only
    human-written statement of *purpose* a page publishes.

    What is absent is as load-bearing as what is present, and each absence was paid for.
    ``context`` costs 14–19 points and is the largest finding in ``the-input-is-the-ceiling``.
    ``value`` costs 1.4% [1.2%, 1.7%] — it was in the first cut of this function despite an
    earlier sweep having already measured it as harmful, and the harness caught it. ``role``
    costs 0.8% [0.3%, 1.3%]. ``landmark``, ``id``, ``class`` and ``data-*`` are machine tokens
    that cost 11 points of exact-label accuracy and bought nothing.

    The caveat on ``role`` is worth keeping in view rather than treating as settled: no query
    family in the harness ever names a kind of control ("the search *button*"), which is the one
    thing a role is for, so the measurement showing it harmful is taken on queries that could
    never have rewarded it. The role still reaches the model — it travels in the payload, and
    ``find_one`` can filter on it exactly. What the logged queries say about how often a real
    query names a control kind is the evidence that should reopen this."""
    parts = (name, url_in_words(url), title)
    written = _without_repeated_words(" ".join(part for part in parts if part).strip())
    # `value` is a fallback, never an addition — see `text_or_fallback`.
    return text_or_fallback(written, value)


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


class WeakAnchor(Exception):
    """Raised when the element a caller anchored to could not itself be found confidently.

    An anchored search is two searches, and the second one can miss. When it does, joining on it
    is worse than not anchoring at all: the ranking is then organised around whatever the anchor
    words happened to hit, and the answer comes back with the same confident face as a correct
    one. Measured on 284 anchored cases, refusing when the anchor's own margin falls below 2%
    catches a third of the failures and costs one correct answer in 242."""

    def __init__(self, anchor: str, margin: float) -> None:
        super().__init__(f"the anchor {anchor!r} matched nothing clearly (margin {margin:.3f})")
        self.anchor = anchor
        self.margin = margin


def _tree_path(identifier: str) -> tuple[str, ...]:
    """An element id as a path through the tree, or ``()`` when it is not one.

    The native surface addresses elements by their route from the window, so its ids *are* the
    structure. The browser addresses them by aria-ref, which says nothing about where a thing
    sits — so proximity is simply unavailable there, and an anchored search degrades to an
    ordinary one rather than to a wrong one."""
    parts = identifier.split(".")
    return tuple(parts) if len(parts) > 1 and all(part.isdigit() for part in parts) else ()


def intent(query: str) -> Any:
    """A query as a unit vector, for asking whether a later one restates it.

    Comparing the *words* of two queries answers nothing: restating something means changing the
    words, and two attempts at one idea shared as little as two tokens out of five in a recorded
    session. The model already holds the notion of sameness this needs, so the comparison happens
    where it lives. Returns ``None`` when the model is unavailable, and callers treat that as "no
    opinion" rather than falling back to a text test that is known not to work."""
    model = _dense_model()
    if model is None:
        return None
    import numpy as np

    vector = np.asarray(model.encode([query], show_progress_bar=False)[0], dtype=np.float32)
    return vector / max(float(np.linalg.norm(vector)), 1e-9)


def _closeness(candidate: str, anchor: str) -> float:
    """How near two elements sit in the tree, from 0 (only the window in common) to 1 (siblings).

    Shared prefix over the *longer* of the two paths. Dividing by the candidate's own depth was
    the obvious reading — "how much of this element's position is explained by the anchor" — and
    it is wrong in a way only a real window showed: a shallow element shares the root with
    everything, so at depth two it scores 0.5 against every anchor on the surface and outranks the
    deep siblings that are actually beside it. In VS Code that handed the same toolbar button back
    for five different anchors. Over the longer path, a shallow element scores near zero against a
    deep anchor, which is the truth — they are not near each other in any sense a person means."""
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
        #: Indices the ranker cannot represent — see :meth:`_unrepresentable_indices`. Filled in
        #: when the matrix is built, because zero-ness is a fact about the encoding rather than
        #: about the text, and only the model knows it.
        self._unreachable: set[int] = set()

    def _unrepresentable_indices(self, vectors: Any) -> set[int]:
        """Documents whose key encodes to the zero vector, and are therefore reachable by nothing.

        An icon font draws its glyphs from the Unicode private use area — VS Code's ``\\uea9b``,
        and 129 of its 377 elements. Those codepoints mean nothing to any tokenizer, so the key
        encodes to all zeros: cosine against *every* query is exactly 0.000, including against the
        glyph itself, which ranked 256th of 260 when asked for by its own text. This is not a
        ranking failure to be tuned. There is no query, in any wording, that reaches such an
        element, and keeping it in the listing offers the model something to aim at that can never
        be found again.

        Measured across twelve live applications: 149 of 1,135 elements (13.1%), concentrated
        entirely in the Chromium ones — 34% of VS Code, 40% of Wave, 1% of Photos, none anywhere
        else. Removing them takes exact-text retrieval from 93% to **100%**, because every one of
        the 7% that failed was one of these.

        They remain perfectly actionable through their id, which is what ``parent`` and the
        element's position are for. What they are not is *searchable*."""
        import numpy as np

        norms = np.linalg.norm(vectors, axis=1)
        return {index for index in range(len(self.documents)) if float(norms[index]) < 1e-6}

    def _dense_scores(self, query: str) -> Optional[list[float]]:
        model = _dense_model()
        if model is None or not self.documents:
            return None
        import numpy as np

        if self._dense_matrix is None:
            vectors = np.asarray(model.encode([document.text for document in self.documents], show_progress_bar=False), dtype=np.float32)
            self._unreachable = self._unrepresentable_indices(vectors)
            if self._unreachable:
                logger.info("screen index: %d of %d keys encode to nothing and are unsearchable",
                            len(self._unreachable), len(self.documents))
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            self._dense_matrix = vectors / np.clip(norms, 1e-9, None)
        query_vector = np.asarray(model.encode([query], show_progress_bar=False)[0], dtype=np.float32)
        query_vector = query_vector / max(float(np.linalg.norm(query_vector)), 1e-9)
        return (self._dense_matrix @ query_vector).tolist()

    def search(self, query: str, *, top_k: int, floor: float = 0.0) -> list[Hit]:
        """Rank the surface against ``query`` and return its ``top_k`` best matches, dropping any
        that score below ``floor``.

        There used to be an ``everything`` flag here that returned the whole ranking and ignored
        ``top_k``, for "bulk harvest". It was a mistake in two directions at once. A caller writing
        ``find_many(query, limit=20, all=True)`` — which reads like "up to 20, exhaustively" — got
        590 elements and 1.5MB, because the two parameters contradicted each other and the flag
        won silently. And the premise was wrong anyway: the tail of a ranking is not more answer,
        it is the surface in score order, and on the page that ended a turn the 590th element
        scored -0.12 against the query that returned it.

        ``floor`` is what lets this method say *nothing matched*, which nothing above it could
        express before. A cosine is comparable across queries on one surface, so an absolute floor
        means the same thing from call to call; under the BM25 fallback the scores are unbounded
        sums with no such calibration, so there it is applied relative to the best score in the
        ranking, which is the closest thing that ranker has to the same statement.

        What the floor is *not* is an absence detector, and the distinction is measured. Across
        596 elements of one real page, paraphrases of things present scored 0.48–0.75 at top-1 and
        queries for things plainly absent scored 0.26–0.59. Those overlap, so there is no cutoff
        that admits every real match and refuses every absent one. The floor is therefore set to
        clear the band that is unambiguously noise and nothing more; an empty result means nothing
        rose above that band, which is weaker than "it is not there" and must be read as such.

        The embedding model ranks. BM25 is the fallback for when it cannot load, and nothing
        more, because measurement said so: across 39 labelled queries on four applications,
        twelve fusion strategies were compared, and ranking by the model alone won on every
        family — including exact-match queries, where BM25 was supposed to be indispensable and
        scored 13/13 either way. Fusing the two was worse than either alone (MRR 0.685 against
        0.853), the signature of a fusion doing harm. Reciprocal rank fusion was the specific
        culprit: it credited every document by rank position, so one with no overlap at all
        still earned 65% of a perfect match's score, and 13 of 39 right answers fell out of the
        top three entirely.

        Keys the ranker cannot represent are dropped rather than ranked — see
        :meth:`_unrepresentable_indices`. They would otherwise sit at a flat 0.0 against every
        query and be ordered by nothing but their position in the tree."""
        if not self.documents:
            return []
        dense_scores = self._dense_scores(query)
        if dense_scores is not None:
            fused, unreachable = dense_scores, self._unreachable
            # A zero floor means "no floor", not "drop everything below zero": `find_one` ranks
            # without one and its margin test was calibrated against the full ranking, so the
            # default must leave that ranking exactly as it was.
            cutoff = floor if floor > 0 else float("-inf")
        else:
            # Without the model, BM25 carries retrieval — and there the same elements are
            # unreachable for the same reason by a different route: a private-use glyph yields no
            # tokens at all, so it can never share one with a query.
            fused = self._bm25.scores(_tokens(query))
            unreachable = {index for index, document in enumerate(self.documents)
                           if not _tokens(document.text)}
            best = max(fused, default=0.0)
            cutoff = floor * best if floor > 0 and best > 0 else float("-inf")
        order = [index for index in _ranked_indices(fused)
                 if index not in unreachable and fused[index] >= cutoff][:top_k]
        return [Hit(id=self.documents[index].id, score=fused[index], payload=self.documents[index].payload) for index in order]

    def anchored(self, query: str, near: str, *, top_k: int, weight: float,
                 anchor_margin: float) -> list[Hit]:
        """Rank against ``query``, preferring elements that sit near whatever ``near`` matches.

        Two searches over the same index, joined by structure rather than by words — which is the
        only thing that separates elements whose words are identical. Fourteen buttons all reading
        "Close" are one query and fourteen answers; "the close button near dispatch.py" is one.

        **Both halves of the score are kept, and that is the whole design.** Ranking by proximity
        alone scores 21.5% on the cases this exists for — barely above the 20.8% of not anchoring
        at all — because the elements nearest an anchor are mostly its own siblings rather than
        the thing being asked for. Relevance alone is that same 20.8%. Together they are **85.2%**
        with a realistically-searched anchor, measured over 284 anchored cases on ten live
        applications.

        Added rather than multiplied. Both forms reach 89.4% on the cases they are for, but a
        caller will sometimes anchor a query that did not need it, and there the additive form
        costs 2.6 points where the multiplicative costs 6.0 (87.0% against 83.6%). The gentler
        failure is worth the identical success.

        The anchor is never returned as its own answer: it is the thing being pointed *from*."""
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
        relevance = self._dense_scores(query) or self._bm25.scores(_tokens(query))
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

