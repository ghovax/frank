"""The queries a strategy is scored against, and where each family's wording comes from."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from langmesh.computer.retrieval import url_in_words

from tests.retrieval.corpus import Corpus, RecordedElement

# Every family yields every query the page supports — nothing is sampled and there is no seed.


@dataclass(frozen=True)
class Query:
    """One query and the index, within its corpus, of the element it should find."""

    text: str
    target_index: int


QueryFamily = Callable[[Corpus], list[Query]]


def _usable_name(element: RecordedElement) -> bool:
    return bool(element.name.strip())


def literal_queries(corpus: Corpus) -> list[Query]:
    """The element's own accessible name, exactly as the page states it."""
    return [Query(text=element.name, target_index=index)
            for index, element in enumerate(corpus.elements)
            if _usable_name(element) and len(element.name) <= 60]


def partial_queries(corpus: Corpus) -> list[Query]:
    """A real three-word run taken from inside a long label."""
    queries = []
    for index, element in enumerate(corpus.elements):
        words = element.name.split()
        if len(words) < 5:
            continue
        queries.append(Query(text=" ".join(words[1:4]), target_index=index))
    return queries


def slug_queries(corpus: Corpus) -> list[Query]:
    """The readable words of a link's destination, when they share no word with its label."""
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
    """The element's tooltip, when its words are not already its label."""
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

# The field each family's wording is drawn from, for the families where it discriminates between strategies.
CIRCULAR_FIELD_BY_FAMILY = {"tooltip": "title", "slug": "url"}
