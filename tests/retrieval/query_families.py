"""The queries a strategy is scored against, and where each family's wording comes from.

Every query here is text a human wrote in the product being searched. None is invented by whoever
wrote the harness, which is a rule rather than a preference: an earlier round of this
investigation scored strategies against hand-written "semantic" queries and measured nothing but
the imagination of the person who wrote them.

The four families deliberately test different things, because a single blended number hides the
trade a strategy is actually making.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from frank.computer.retrieval import url_in_words

from tests.retrieval.corpus import Corpus, RecordedElement

# Every family yields every query the page supports — nothing is sampled and there is no seed.
#
# This is a correction to how these measurements were first taken. Capping each family at a fixed
# count per site silently *imposes* equal weighting across families, and that weighting is exactly
# the assumption the shipped key's margin rests on and the one thing here with no evidence behind
# it. Taking everything lets the pages decide the mix instead: a corpus where few links have
# distinctive URLs contributes few slug queries, which is a fact about such pages rather than a
# knob. It also removes the sampling seed, so a change in a number can only mean a change in the
# code.


@dataclass(frozen=True)
class Query:
    """One query and the index, within its corpus, of the element it should find."""

    text: str
    target_index: int


QueryFamily = Callable[[Corpus], list[Query]]


def _usable_name(element: RecordedElement) -> bool:
    return bool(element.name.strip())


def literal_queries(corpus: Corpus) -> list[Query]:
    """The element's own accessible name, exactly as the page states it.

    The easy case, and the one most likely to be common in practice: a model that can see a label
    and asks for it by name."""
    return [Query(text=element.name, target_index=index)
            for index, element in enumerate(corpus.elements)
            if _usable_name(element) and len(element.name) <= 60]


def partial_queries(corpus: Corpus) -> list[Query]:
    """A real three-word run taken from inside a long label.

    Stands in for a model that half-remembers what it saw, or that quotes the part of a long link
    that mattered to it rather than the whole thing.

    The window starts at the second word rather than at a random offset. Skipping the first word
    is the point: a query beginning at the start of a label can be answered by prefix similarity,
    which is not the ability being tested. Taking a fixed window also means no seed."""
    queries = []
    for index, element in enumerate(corpus.elements):
        words = element.name.split()
        if len(words) < 5:
            continue
        queries.append(Query(text=" ".join(words[1:4]), target_index=index))
    return queries


def slug_queries(corpus: Corpus) -> list[Query]:
    """The readable words of a link's destination, when they share no word with its label.

    The word-overlap filter is what makes this family meaningful. A URL that repeats its link's
    text would be answerable by matching the label alone, and counting those would credit a
    strategy for retrieval it did not perform. What survives is wording chosen by whoever built
    the site's URLs, independently of whoever wrote its link text."""
    queries = []
    for index, element in enumerate(corpus.elements):
        if element.role != "link" or not element.url:
            continue
        phrase = url_in_words(element.url).lower().replace("_", " ").replace("-", " ")
        label = element.name.lower()
        if not phrase or not label:
            continue
        if set(phrase.split()) & set(label.split()):
            continue
        queries.append(Query(text=phrase, target_index=index))
    return queries


def tooltip_queries(corpus: Corpus) -> list[Query]:
    """The element's tooltip, when its words are not already its label.

    The closest thing to a genuine test of meaning that a page supplies without anyone inventing
    it: a developer's own prose description of what a control is for.

    A strategy that indexes the tooltip scores on this family **by construction** — the query is
    the indexed text. That is why :func:`tests.retrieval.evaluation.is_circular_pairing` exists
    and why reports mark the combination rather than quietly ranking it first."""
    queries = []
    for index, element in enumerate(corpus.elements):
        title = element.title.strip()
        if not title or len(title.split()) < 2:
            continue
        if set(title.lower().split()) <= set(element.name.lower().split()):
            continue
        queries.append(Query(text=title, target_index=index))
    return queries


QUERY_FAMILIES: dict[str, QueryFamily] = {
    "literal": literal_queries,
    "partial": partial_queries,
    "slug": slug_queries,
    "tooltip": tooltip_queries,
}

# The field each family's wording is drawn from, for the families where it discriminates between
# strategies. A strategy that indexes the named field is scored circularly by that family and its
# result there is not evidence of retrieval.
#
# `literal` and `partial` are absent on purpose: their wording comes from the element's name, and
# every strategy indexes the name, so flagging them would mark everything and distinguish nothing.
CIRCULAR_FIELD_BY_FAMILY = {"tooltip": "title", "slug": "url"}
