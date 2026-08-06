"""Candidate ways of turning an element into the text an embedding ranks."""

from __future__ import annotations

from typing import Callable, Iterable

from langmesh.computer.retrieval import (
    _ROLE_IN_WORDS,
    _without_repeated_words,
    element_text,
    text_or_fallback,
    url_in_words,
    web_element_text,
)

from tests.retrieval.corpus import RecordedElement

EncodingStrategy = Callable[[RecordedElement], str]
FieldSource = Callable[[RecordedElement], str]


def _role_in_words(element: RecordedElement) -> str:
    """The element's role said as a person would say it, or nothing for an unrecognised role."""
    return _ROLE_IN_WORDS.get(element.role, _ROLE_IN_WORDS.get(element.role.lower(), ""))


# Each field an element can contribute, as the words it would contribute.
FIELD_SOURCES: dict[str, FieldSource] = {
    "name": lambda element: element.name,
    "url": lambda element: url_in_words(element.url),
    "role": _role_in_words,
    "title": lambda element: element.title,
    "context": lambda element: element.context,
    "value": lambda element: element.value,
}


def compose(field_names: Iterable[str]) -> EncodingStrategy:
    """Build a strategy that joins the named fields, in order, dropping repeated words."""
    sources = [FIELD_SOURCES[field_name] for field_name in field_names]

    def strategy(element: RecordedElement) -> str:
        parts = [source(element) for source in sources]
        return _without_repeated_words(" ".join(part for part in parts if part).strip())

    return strategy


def name_of(field_names: Iterable[str]) -> str:
    """The name a composition is reported under: the fields it contains, joined."""
    return " + ".join(field_names)


# The compositions worth comparing.
COMPOSITIONS: tuple[tuple[str, ...], ...] = (
    ("name",),
    ("name", "role"),
    ("name", "url"),
    ("name", "url", "role"),
    ("name", "url", "title"),
    ("name", "url", "role", "title"),
    ("name", "url", "role", "title", "value"),
    ("name", "url", "role", "context"),
    ("name", "url", "role", "title", "context"),
    ("name", "role", "context"),
    ("name", "context"),
)

STRATEGIES: dict[str, EncodingStrategy] = {
    name_of(field_names): compose(field_names) for field_names in COMPOSITIONS
}

# The fields each composition indexes, so a report can mark the families that score it circularly.
FIELDS_BY_STRATEGY: dict[str, frozenset[str]] = {
    name_of(field_names): frozenset(field_names) for field_names in COMPOSITIONS
}

# The key the browser surface builds right now, measured by calling the product rather than by restating it.
LIVE_KEY_NAME = "live browser key"
LIVE_NATIVE_KEY_NAME = "live native key"


def live_browser_key(element: RecordedElement) -> str:
    """Whatever :func:`langmesh.computer.retrieval.web_element_text` currently produces."""
    return web_element_text(name=element.name, url=element.url, title=element.title,
                            value=element.value)


def live_native_key(element: RecordedElement) -> str:
    """What :meth:`langmesh.computer.engine.NativeSurface.documents` currently builds."""
    return text_or_fallback(
        text_or_fallback(element_text(name=element.name), element.value),
        element.role_description,
    )


STRATEGIES[LIVE_KEY_NAME] = live_browser_key
STRATEGIES[LIVE_NATIVE_KEY_NAME] = live_native_key
# Which fields each live key draws on, so a report can mark the families that score it circularly.
FIELDS_BY_STRATEGY[LIVE_KEY_NAME] = frozenset({"name", "url", "title", "value"})
FIELDS_BY_STRATEGY[LIVE_NATIVE_KEY_NAME] = frozenset({"name", "value", "role_description"})
