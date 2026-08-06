"""Measure every composition and save the result as data you can query."""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

import pandas

from tests.retrieval.corpus import load_all_corpora
from tests.retrieval.evaluation import (
    DEFAULT_CANDIDATE_DEPTH,
    DEFAULT_RECALL_DEPTHS,
    build_queries,
    evaluate_strategy,
    is_separable,
    paired_bootstrap_interval,
)
from tests.retrieval.query_families import CIRCULAR_FIELD_BY_FAMILY
from tests.retrieval.strategies import FIELDS_BY_STRATEGY, LIVE_KEY_NAME, STRATEGIES

logger = logging.getLogger(__name__)

RESULTS_DIRECTORY = Path(__file__).parent / "results"


def measure_everything(recall_depths: tuple[int, ...] = DEFAULT_RECALL_DEPTHS,
                       surface_name: str = "web") -> pandas.DataFrame:
    """One row per query per strategy, with the derived per-query columns already attached."""
    corpora = load_all_corpora(surface_name=surface_name)
    if not corpora:
        raise SystemExit(f"no {surface_name} corpora recorded; run "
                         f"`uv run python -m tests.retrieval.harvest`")
    queries = build_queries(corpora)
    rows = []
    for strategy_name, strategy in STRATEGIES.items():
        for outcome in evaluate_strategy(strategy_name, strategy, corpora, queries):
            rows.append({
                **asdict(outcome),
                "found_first": outcome.found_first,
                **{f"found_within_{depth}": outcome.found_within(depth) for depth in recall_depths},
                "reciprocal_rank": outcome.reciprocal_rank,
                "scored_circularly": CIRCULAR_FIELD_BY_FAMILY.get(outcome.family_name)
                in FIELDS_BY_STRATEGY[outcome.strategy_name],
            })
    return pandas.DataFrame(rows)


def corpus_summary(outcomes: pandas.DataFrame) -> pandas.DataFrame:
    """How many queries each site contributed to each family."""
    one_strategy = outcomes[outcomes.strategy_name == LIVE_KEY_NAME]
    return one_strategy.pivot_table(index="site_name", columns="family_name",
                                    values="query_text", aggfunc="count",
                                    margins=True, margins_name="all").fillna(0).astype(int)


def accuracy_by_family(outcomes: pandas.DataFrame,
                       candidate_depth: int = DEFAULT_CANDIDATE_DEPTH) -> pandas.DataFrame:
    """Top-1 accuracy per strategy per family, with recall and mean reciprocal rank beside it."""
    accuracy = outcomes.pivot_table(index="strategy_name", columns="family_name",
                                    values="found_first", aggfunc="mean")
    circular = outcomes.pivot_table(index="strategy_name", columns="family_name",
                                    values="scored_circularly", aggfunc="any")
    formatted = accuracy.map(lambda value: f"{value:.0%}")
    formatted = formatted.where(~circular.fillna(False), formatted + "*")
    overall = outcomes.groupby("strategy_name").agg(
        recall=(f"found_within_{candidate_depth}", "mean"),
        mean_reciprocal_rank=("reciprocal_rank", "mean"),
    )
    formatted[f"recall@{candidate_depth}"] = overall.recall.map(lambda value: f"{value:.0%}")
    formatted["MRR"] = overall.mean_reciprocal_rank.round(3)
    return formatted.sort_values("MRR", ascending=False)


def comparisons_against(outcomes: pandas.DataFrame, reference_name: str) -> pandas.DataFrame:
    """Every strategy's paired difference from one reference, with bootstrap intervals."""
    ordered = outcomes.sort_values(["site_name", "family_name", "query_text"])
    hits_by_strategy = {
        strategy_name: group.found_first.to_numpy(dtype=float)
        for strategy_name, group in ordered.groupby("strategy_name")
    }
    reference_hits = hits_by_strategy[reference_name]
    rows = []
    for strategy_name, hits in hits_by_strategy.items():
        if strategy_name == reference_name:
            continue
        interval = paired_bootstrap_interval(hits, reference_hits)
        difference, low, high = interval
        rows.append({
            "strategy_name": strategy_name,
            "difference": difference,
            "interval_low": low,
            "interval_high": high,
            "verdict": "separable" if is_separable(interval) else "indistinguishable",
        })
    return pandas.DataFrame(rows).sort_values("difference", ascending=False).set_index("strategy_name")


def recall_curve(outcomes: pandas.DataFrame,
                 recall_depths: tuple[int, ...] = DEFAULT_RECALL_DEPTHS) -> pandas.DataFrame:
    """Recall at each depth, per strategy: whether misses are absences or mis-orderings."""
    columns = {f"recall@{depth}": (f"found_within_{depth}", "mean") for depth in recall_depths}
    curve = outcomes.groupby("strategy_name").agg(**columns)
    return curve.map(lambda value: f"{value:.0%}").loc[
        outcomes.groupby("strategy_name").reciprocal_rank.mean().sort_values(ascending=False).index
    ]


def main() -> int:
    outcomes = measure_everything()
    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = RESULTS_DIRECTORY / "query_outcomes.parquet"
    try:
        outcomes.to_parquet(destination)
    except Exception:  # noqa: BLE001 — parquet needs an engine installed; csv always works
        destination = destination.with_suffix(".csv")
        outcomes.to_csv(destination, index=False)

    pandas.set_option("display.width", 200)
    pandas.set_option("display.max_columns", 24)

    strategy_count = outcomes.strategy_name.nunique()
    logger.info("%d strategies x %d queries = %d measured outcomes, saved to %s",
                strategy_count, len(outcomes) // strategy_count, len(outcomes), destination)
    logger.info("queries contributed per site and family\n%s", corpus_summary(outcomes).to_string())
    logger.info("top-1 accuracy by family (* = scored circularly, not evidence of retrieval)\n%s",
                accuracy_by_family(outcomes).to_string())
    logger.info("recall by depth - a flat curve means elements are missing, a rising one "
                "means they are merely mis-ranked\n%s", recall_curve(outcomes).to_string())
    comparisons = comparisons_against(outcomes, LIVE_KEY_NAME)
    printable = comparisons.assign(
        difference=comparisons.difference.map(lambda value: f"{value:+.1%}"),
        interval=comparisons.apply(
            lambda row: f"[{row.interval_low:+.1%}, {row.interval_high:+.1%}]", axis=1),
    )
    logger.info("paired difference from %r, 95%% bootstrap interval\n%s",
                LIVE_KEY_NAME, printable[["difference", "interval", "verdict"]].to_string())
    return 0


if __name__ == "__main__":
    # Configured here rather than at import, so that importing this module for its functions never reconfigures logging for whoever imported it.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
