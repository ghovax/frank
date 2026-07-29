"""Candidate ways of turning an element into the text an embedding ranks.

A strategy here is a **composition of fields**, named after the fields it contains and nothing
else. There is deliberately no "current" or "previous" strategy: which composition a surface
happens to use is a fact about today, and a harness that encodes today's choice in its vocabulary
stops being able to measure tomorrow's. Tests accordingly assert things about *fields* — that
adding a section label costs accuracy, that a link's destination earns its place — which stay
true and stay checkable however the product is put together.

The live key is measured too, under :data:`LIVE_KEY_NAME`, by calling the product's own function.
That is a reference rather than a baseline: it tracks whatever the surface currently builds, so a
change to the product shows up here as a moved number instead of a stale copy.

Adding a candidate means adding a tuple to :data:`COMPOSITIONS`; nothing else needs to change.
"""

from __future__ import annotations

from typing import Callable, Iterable

from frank.computer.retrieval import _ROLE_IN_WORDS, _without_repeated_words, url_in_words, web_element_text

from tests.retrieval.corpus import RecordedElement

EncodingStrategy = Callable[[RecordedElement], str]
FieldSource = Callable[[RecordedElement], str]


def _role_in_words(element: RecordedElement) -> str:
    """The element's role said as a person would say it, or nothing for an unrecognised role."""
    return _ROLE_IN_WORDS.get(element.role, _ROLE_IN_WORDS.get(element.role.lower(), ""))


# Each field an element can contribute, as the words it would contribute. Adding an entry here
# makes a new field available to every composition below and to any future one.
FIELD_SOURCES: dict[str, FieldSource] = {
    "name": lambda element: element.name,
    "url": lambda element: url_in_words(element.url),
    "role": _role_in_words,
    "title": lambda element: element.title,
    "context": lambda element: element.context,
    "value": lambda element: element.value,
}


def compose(field_names: Iterable[str]) -> EncodingStrategy:
    """Build a strategy that joins the named fields, in order, dropping repeated words.

    Deduplication is part of the composition rather than a variant of it because a repeated word
    is weight in the embedding without being information, and no measurement has ever favoured
    keeping the repeats."""
    sources = [FIELD_SOURCES[field_name] for field_name in field_names]

    def strategy(element: RecordedElement) -> str:
        parts = [source(element) for source in sources]
        return _without_repeated_words(" ".join(part for part in parts if part).strip())

    return strategy


def name_of(field_names: Iterable[str]) -> str:
    """The name a composition is reported under: the fields it contains, joined."""
    return " + ".join(field_names)


# The compositions worth comparing. Each exists to isolate one question, and the set is designed
# so that every field appears both present and absent alongside an otherwise identical partner —
# without that pairing, a difference cannot be attributed to the field that caused it.
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

# The key the browser surface builds right now, measured by calling the product rather than by
# restating it. Named for what it is — a live reference — so that nothing here has to be renamed
# when the composition behind it changes.
LIVE_KEY_NAME = "live browser key"


def live_browser_key(element: RecordedElement) -> str:
    """Whatever :func:`frank.computer.retrieval.web_element_text` currently produces."""
    return web_element_text(name=element.name, role=element.role, url=element.url,
                            title=element.title)


STRATEGIES[LIVE_KEY_NAME] = live_browser_key
# Which fields the live key draws on, so a report can mark the families that score it circularly.
# Kept in step with `live_browser_key` above by the drift test in `test_encoding_strategies`.
FIELDS_BY_STRATEGY[LIVE_KEY_NAME] = frozenset({"name", "url", "role", "title"})
