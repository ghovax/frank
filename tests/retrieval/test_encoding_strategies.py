"""Guards on the retrieval key: the findings from ``the-input-is-the-ceiling``, as assertions."""

from __future__ import annotations

from dataclasses import fields

import numpy
import pytest

from tests.retrieval.corpus import RecordedElement, load_all_corpora
from tests.retrieval.evaluation import build_queries, evaluate_strategy, is_separable, paired_bootstrap_interval
from tests.retrieval.strategies import FIELD_SOURCES, LIVE_KEY_NAME, STRATEGIES, name_of


# These assertions are about the browser key, so they load only browser corpora.
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
    """A family that silently yields nothing turns its column into a false pass."""
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
    """The largest single finding, checked against three different bases."""
    interval = paired_bootstrap_interval(hits_of(outcomes, name_of(with_context)),
                                         hits_of(outcomes, name_of(without)))
    difference, low, high = interval
    assert difference < 0, (
        f"adding context to {name_of(without)!r} no longer costs accuracy "
        f"(difference {difference:+.1%}, interval [{low:+.1%}, {high:+.1%}]) — re-run "
        f"`tests.retrieval.report` before trusting this"
    )


def test_link_destinations_carry_retrieval_signal_of_their_own(outcomes):
    """Parsing ``/url:`` was the change that separated the top compositions from the rest."""
    with_url = accuracy_on(outcomes, name_of(("name", "url", "role")), "slug")
    without_url = accuracy_on(outcomes, name_of(("name", "role")), "slug")
    assert with_url > without_url + 0.10, (
        f"URL words no longer help destination queries: {with_url:.0%} with, {without_url:.0%} without"
    )


def test_the_tooltip_does_not_cost_accuracy_elsewhere(outcomes):
    """The tooltip's value on its own family is circular, so it is judged on the other families."""
    with_title = name_of(("name", "url", "role", "title"))
    without_title = name_of(("name", "url", "role"))
    for family_name in ("literal", "partial", "slug"):
        cost = accuracy_on(outcomes, without_title, family_name) - accuracy_on(outcomes, with_title, family_name)
        assert cost < 0.05, (
            f"carrying the tooltip now costs {cost:.0%} on {family_name!r} queries, which was the "
            f"condition it was adopted under"
        )


def test_machine_tokens_are_not_available_as_fields():
    """``id``, ``class`` and ``data-*`` cost 11 points of exact-label accuracy and bought nothing."""
    forbidden = {"id", "class", "data", "dataset", "test_id", "testid"}
    available_fields = set(FIELD_SOURCES) | {field.name for field in fields(RecordedElement)}
    assert not (available_fields & forbidden), (
        f"machine tokens are reachable again as {sorted(available_fields & forbidden)} — they were "
        f"measured as costing accuracy and buying nothing"
    )


def test_the_live_key_is_judged_on_reach_as_well_as_rank(outcomes, corpora):
    """The live key may rank slightly below a composition of its own fields — but only by reaching further, never by being worse at the same job."""
    from tests.retrieval.strategies import live_browser_key

    equivalent = name_of(("name", "url", "title"))
    interval = paired_bootstrap_interval(hits_of(outcomes, LIVE_KEY_NAME), hits_of(outcomes, equivalent))
    difference, low, high = interval

    unreachable_live = unreachable_composed = 0
    for corpus in corpora:
        for element in corpus.elements:
            if not (element.name.strip() or element.value.strip()):
                continue
            unreachable_live += int(not live_browser_key(element).strip())
            unreachable_composed += int(not STRATEGIES[equivalent](element).strip())

    if is_separable(interval) and difference < 0:
        assert unreachable_live < unreachable_composed, (
            f"the live key ranks {difference:+.1%} [{low:+.1%}, {high:+.1%}] below {equivalent!r} "
            f"and reaches no further ({unreachable_live} unreachable against "
            f"{unreachable_composed}) — a loss with nothing bought"
        )


def test_the_role_signal_in_the_embedding_stays_too_weak_to_act_on(corpora):
    """Same-role elements sit measurably closer — and nowhere near closely enough."""
    from model2vec import StaticModel
    import numpy

    from tests.retrieval.strategies import compose

    model = StaticModel.from_pretrained("minishlab/M2V_multilingual_output")
    build_text = compose(("name", "url", "title"))
    cohesions = []
    for corpus in corpora:
        elements = corpus.elements[:400]
        if len(elements) < 50:
            continue
        vectors = numpy.asarray(model.encode([build_text(element) or " " for element in elements],
                                             show_progress_bar=False), dtype=numpy.float32)
        # One sample, used for both the matrix and the pairing below.
        vectors /= numpy.maximum(numpy.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)
        similarity = vectors @ vectors.T
        same, different = [], []
        for first in range(len(elements)):
            for second in range(first + 1, len(elements)):
                bucket = same if elements[first].role == elements[second].role else different
                bucket.append(similarity[first, second])
        if same and different:
            cohesions.append(float(numpy.mean(same) - numpy.mean(different)))
    assert cohesions, "no corpus large enough to measure role cohesion"
    cohesion = float(numpy.mean(cohesions))
    assert 0.02 < cohesion < 0.25, (
        f"same-role cohesion is now {cohesion:+.3f}, outside the range this design assumes. Below "
        f"0.02 the embedding has stopped encoding role at all; above 0.25 similarity may isolate a "
        f"role unaided and faceted narrowing deserves re-examining"
    )


def test_no_element_carrying_words_is_left_unreachable(corpora):
    """An element with words to offer must produce a non-empty key."""
    from langmesh.computer.retrieval import web_element_text

    unreachable = []
    for corpus in corpora:
        for element in corpus.elements:
            if not (element.name.strip() or element.value.strip()):
                continue  # genuinely has nothing to say; nothing to be done for it
            key = web_element_text(name=element.name, url=element.url, title=element.title,
                                   value=element.value)
            if not key.strip():
                unreachable.append((corpus.site_name, element.role, element.value[:40]))
    assert not unreachable, (
        f"{len(unreachable)} elements carry words but produce an empty key, so no query can reach "
        f"them: {unreachable[:3]}"
    )
