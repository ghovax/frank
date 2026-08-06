"""Scoring a strategy, and deciding whether two strategies actually differ."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from langmesh.computer.retrieval import Document, Index

from tests.retrieval.corpus import Corpus
from tests.retrieval.query_families import QUERY_FAMILIES, Query
from tests.retrieval.strategies import EncodingStrategy

# Defaults, not settings.

# The depth `find_one` considers before deciding, from `runtime/tools/dispatch.py`, where it takes `scored[:5]` and picks the score-competitive one.
DEFAULT_CANDIDATE_DEPTH = 5
DEFAULT_RECALL_DEPTHS = (1, 3, 5, 10)

# Resamples for the paired bootstrap: enough that an interval is stable to a tenth of a point between runs.
DEFAULT_BOOTSTRAP_RESAMPLES = 4000

# Fixed only so an interval does not shift between two runs over identical data.
DEFAULT_BOOTSTRAP_SEED = 20260729


@dataclass(frozen=True)
class QueryOutcome:
    """What one strategy did with one query: the row everything else is derived from."""

    strategy_name: str
    site_name: str
    family_name: str
    query_text: str
    rank: int | None
    """Zero-based position of the right element in the full ranking, or ``None`` if unranked.

    The full rank is stored rather than a verdict at some depth, so that any depth can be asked
    for afterwards without re-running the sweep."""

    @property
    def found_first(self) -> bool:
        return self.rank == 0

    def found_within(self, depth: int) -> bool:
        """Whether the right element was inside the first ``depth`` results."""
        return self.rank is not None and self.rank < depth

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / (self.rank + 1) if self.rank is not None else 0.0


def build_queries(corpora: list[Corpus]) -> dict[str, dict[str, list[Query]]]:
    """Every family's queries for every corpus, built once and shared by all strategies."""
    return {
        corpus.site_name: {
            family_name: build_family(corpus)
            for family_name, build_family in QUERY_FAMILIES.items()
        }
        for corpus in corpora
    }


def evaluate_strategy(strategy_name: str, strategy: EncodingStrategy, corpora: list[Corpus],
                      queries: dict[str, dict[str, list[Query]]]) -> list[QueryOutcome]:
    """Rank every corpus with one strategy and record what happened to every query."""
    outcomes: list[QueryOutcome] = []
    for corpus in corpora:
        index = Index([
            Document(id=str(position), text=strategy(element), payload={})
            for position, element in enumerate(corpus.elements)
        ])
        for family_name, family_queries in queries[corpus.site_name].items():
            for query in family_queries:
                # The whole ranking, because this measures *where* the target lands rather than whether it is returned.
                ranking = index.search(query.text, top_k=len(corpus.elements))
                positions = [position for position, hit in enumerate(ranking)
                             if hit.id == str(query.target_index)]
                outcomes.append(QueryOutcome(
                    strategy_name=strategy_name,
                    site_name=corpus.site_name,
                    family_name=family_name,
                    query_text=query.text,
                    rank=positions[0] if positions else None,
                ))
    return outcomes


def paired_bootstrap_interval(candidate_hits: np.ndarray, baseline_hits: np.ndarray,
                              confidence: float = 0.95,
                              resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
                              seed: int = DEFAULT_BOOTSTRAP_SEED) -> tuple[float, float, float]:
    """The mean difference in top-1 accuracy and its confidence interval, resampled in pairs."""
    if candidate_hits.size != baseline_hits.size:
        raise ValueError("a paired comparison needs both strategies scored on the same queries")
    differences = candidate_hits - baseline_hits
    generator = np.random.default_rng(seed)
    resampled_means = np.array([
        differences[generator.integers(0, differences.size, differences.size)].mean()
        for _ in range(resamples)
    ])
    tail = (1.0 - confidence) / 2.0 * 100.0
    low, high = np.percentile(resampled_means, [tail, 100.0 - tail])
    return float(differences.mean()), float(low), float(high)


def is_separable(interval: tuple[float, float, float]) -> bool:
    """Whether a bootstrap interval excludes zero, and so supports claiming a real difference."""
    _difference, low, high = interval
    return low > 0.0 or high < 0.0
