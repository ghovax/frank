"""Guards on the retrieval key: the findings from ``the-input-is-the-ceiling``, as assertions.

These exist to stop a settled question from being quietly reopened. Each corresponds to a result
that cost real measurement to reach, and each fails with a message naming the finding rather than
only the number, so whoever hits it learns why the code is the way it is.

Every assertion is about a **field**, not about whichever composition a surface happens to use
today. "Adding the section label costs accuracy" stays checkable when the key is rearranged; "the
current key beats the old one" would not. The live key is checked separately, and only for not
having drifted behind the best composition available to it.

Thresholds are deliberately loose. The point is to catch a reversal, not to pin an accuracy that
would break the moment the fixtures are refreshed.
"""

from __future__ import annotations

from dataclasses import fields

import numpy
import pytest

from tests.retrieval.corpus import RecordedElement, load_all_corpora
from tests.retrieval.evaluation import build_queries, evaluate_strategy, is_separable, paired_bootstrap_interval
from tests.retrieval.strategies import FIELD_SOURCES, LIVE_KEY_NAME, STRATEGIES, name_of


# These assertions are about the browser key, so they load only browser corpora. Pooling the two
# surfaces would score compositions on native windows that cannot supply a URL or a tooltip, which
# would dilute exactly the fields under test.
MEASURED_SURFACE = "web"


@pytest.fixture(scope="module")
def corpora():
    loaded = load_all_corpora(surface_name=MEASURED_SURFACE)
    if not loaded:
        pytest.skip("no browser corpora recorded; run `uv run python -m tests.retrieval.harvest`")
    return loaded


@pytest.fixture(scope="module")
def queries(corpora):
    return build_queries(corpora)


@pytest.fixture(scope="module")
def outcomes(corpora, queries):
    """Every strategy scored once, shared by the tests so the embedding model loads a single time."""
    return {
        strategy_name: evaluate_strategy(strategy_name, strategy, corpora, queries)
        for strategy_name, strategy in STRATEGIES.items()
    }


def hits_of(outcomes, strategy_name: str) -> numpy.ndarray:
    """One strategy's per-query top-1 outcomes, ordered so two strategies can be paired."""
    ordered = sorted(outcomes[strategy_name],
                     key=lambda outcome: (outcome.site_name, outcome.family_name, outcome.query_text))
    return numpy.array([float(outcome.found_first) for outcome in ordered])


def accuracy_on(outcomes, strategy_name: str, family_name: str) -> float:
    """One strategy's top-1 accuracy on one family."""
    relevant = [outcome for outcome in outcomes[strategy_name] if outcome.family_name == family_name]
    return sum(outcome.found_first for outcome in relevant) / len(relevant) if relevant else 0.0


def test_corpora_are_varied_enough_to_measure(corpora):
    """A handful of pages from one site would measure that site, not the encoding."""
    assert len(corpora) >= 4, "fewer than four sites recorded; the sample is too narrow to trust"
    assert sum(len(corpus) for corpus in corpora) >= 1000


def test_every_query_family_is_populated(queries):
    """A family that silently yields nothing turns its column into a false pass.

    This guards the failure mode that wasted the most time in this investigation: a harness that
    skipped a corpus and reported the remainder as though it were complete."""
    totals: dict[str, int] = {}
    for families in queries.values():
        for family_name, family_queries in families.items():
            totals[family_name] = totals.get(family_name, 0) + len(family_queries)
    empty = [family_name for family_name, count in totals.items() if count == 0]
    assert not empty, f"query families produced nothing: {empty}"


@pytest.mark.parametrize("without, with_context", [
    (("name", "url", "role"), ("name", "url", "role", "context")),
    (("name", "role"), ("name", "role", "context")),
    (("name",), ("name", "context")),
])
def test_adding_the_section_label_to_the_key_costs_accuracy(outcomes, without, with_context):
    """The largest single finding, checked against three different bases.

    A section's label reads as disambiguation and behaves as its opposite: written into every
    child's key, it makes those children indistinguishable to a cosine. Parametrised so the result
    cannot be an artefact of one particular composition."""
    interval = paired_bootstrap_interval(hits_of(outcomes, name_of(with_context)),
                                         hits_of(outcomes, name_of(without)))
    difference, low, high = interval
    assert difference < 0, (
        f"adding context to {name_of(without)!r} no longer costs accuracy "
        f"(difference {difference:+.1%}, interval [{low:+.1%}, {high:+.1%}]) — re-run "
        f"`tests.retrieval.report` before trusting this"
    )


def test_link_destinations_carry_retrieval_signal_of_their_own(outcomes):
    """Parsing ``/url:`` was the change that separated the top compositions from the rest.

    Scored only on the slug family, whose wording shares no word at all with the visible label, so
    a strategy cannot pass by matching the label instead."""
    with_url = accuracy_on(outcomes, name_of(("name", "url", "role")), "slug")
    without_url = accuracy_on(outcomes, name_of(("name", "role")), "slug")
    assert with_url > without_url + 0.10, (
        f"URL words no longer help destination queries: {with_url:.0%} with, {without_url:.0%} without"
    )


def test_the_tooltip_does_not_cost_accuracy_elsewhere(outcomes):
    """The tooltip's value on its own family is circular, so it is judged on the other families.

    Capturing it was a judgement call rather than a measured one — the only benchmark for a
    description is the description itself. What *can* be checked is that carrying it does no harm
    where the scoring is honest, which is the condition the decision was made under."""
    with_title = name_of(("name", "url", "role", "title"))
    without_title = name_of(("name", "url", "role"))
    for family_name in ("literal", "partial", "slug"):
        cost = accuracy_on(outcomes, without_title, family_name) - accuracy_on(outcomes, with_title, family_name)
        assert cost < 0.05, (
            f"carrying the tooltip now costs {cost:.0%} on {family_name!r} queries, which was the "
            f"condition it was adopted under"
        )


def test_machine_tokens_are_not_available_as_fields():
    """``id``, ``class`` and ``data-*`` cost 11 points of exact-label accuracy and bought nothing.

    Asserted against the field vocabulary rather than against rendered keys. Matching on the text
    a key produces looks stricter and is in fact wrong: a Wikipedia citation's visible label
    genuinely contains ``government-data-reveals-accessible-homes``, which is page content and not
    a DOM attribute leaking through. What actually needs guarding is that no encoding can *source*
    these attributes, and the recorder does not carry them in the first place."""
    forbidden = {"id", "class", "data", "dataset", "test_id", "testid"}
    available_fields = set(FIELD_SOURCES) | {field.name for field in fields(RecordedElement)}
    assert not (available_fields & forbidden), (
        f"machine tokens are reachable again as {sorted(available_fields & forbidden)} — they were "
        f"measured as costing accuracy and buying nothing"
    )


def test_the_live_key_has_not_fallen_behind_its_own_fields(outcomes):
    """The live key should not be separably worse than the composition of the same fields.

    This is the drift guard: it says nothing about which composition is right, only that whatever
    the surface builds today still performs like a deliberate arrangement of the fields it uses
    rather than an accident."""
    equivalent = name_of(("name", "url", "title"))
    interval = paired_bootstrap_interval(hits_of(outcomes, LIVE_KEY_NAME), hits_of(outcomes, equivalent))
    difference, low, high = interval
    assert not (is_separable(interval) and difference < 0), (
        f"the live key is separably worse than {equivalent!r} by {difference:+.1%} "
        f"[{low:+.1%}, {high:+.1%}]"
    )
