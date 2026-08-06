"""Does an element's *declaration* retrieve better than its words, and does a code model read it?"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy
import pandas
from model2vec import StaticModel

from tests.retrieval.corpus import Corpus, RecordedElement, load_all_corpora
from tests.retrieval.evaluation import (
    DEFAULT_RECALL_DEPTHS,
    build_queries,
    is_separable,
    paired_bootstrap_interval,
)
from tests.retrieval.strategies import compose

logger = logging.getLogger(__name__)

RESULTS_DIRECTORY = Path(__file__).parent / "results"

# Static embedding models to compare.
EMBEDDING_MODELS = {
    "M2V_multilingual (live)": "minishlab/M2V_multilingual_output",
    "potion-retrieval-32M": "minishlab/potion-retrieval-32M",
    "potion-multilingual-128M": "minishlab/potion-multilingual-128M",
    "potion-base-8M": "minishlab/potion-base-8M",
    "potion-code-16M": "minishlab/potion-code-16M",
    "potion-code-16M-v2": "minishlab/potion-code-16M-v2",
}

_OPENING_TAG = re.compile(r"^<[^>]*>")
_ATTRIBUTE = re.compile(r'([a-zA-Z_:][-\w:.]*)\s*=\s*"([^"]*)"')
_TAG_MARKUP = re.compile(r"<[^>]+>")


def opening_tag(element: RecordedElement) -> str:
    """Just the element's own tag with its attributes — the declaration without its children."""
    match = _OPENING_TAG.match(element.source)
    return match.group(0) if match else ""


def attribute_values(element: RecordedElement) -> str:
    """The attribute values alone, without their names or any markup punctuation."""
    return " ".join(value for _name, value in _ATTRIBUTE.findall(opening_tag(element)))[:300]


def markup_text_only(element: RecordedElement) -> str:
    """The declaration with its tags stripped: the words a reader would see, in document order."""
    return " ".join(_TAG_MARKUP.sub(" ", element.source).split())[:300]


def source_declaration(element: RecordedElement) -> str:
    """The element as a declaration: its own markup where the surface published one, otherwise a record assembled here from the attributes it did publish."""
    if element.source:
        return element.source
    parts = [element.role]
    for field_name in ("subrole", "name", "value", "context", "placeholder", "role_description"):
        written = getattr(element, field_name, "")
        if written:
            parts.append(f'{field_name}="{written[:80]}"')
    return " ".join(parts)


def structural_path(element: RecordedElement) -> str:
    return element.path


# How an element is turned into indexed text.
INDEXED_TEXTS = {
    "words: name + url + title (live web)": compose(("name", "url", "title")),
    "words: name (live native)": compose(("name",)),
    "code: full declaration": source_declaration,
    "code: opening tag only": opening_tag,
    "code: attribute values only": attribute_values,
    "code: markup with tags stripped": markup_text_only,
    "structure: ancestor path": structural_path,
    "hybrid: words + declaration": lambda element: f"{compose(('name', 'url', 'title'))(element)} {element.source}".strip(),
    "hybrid: words + attribute values": lambda element: f"{compose(('name', 'url', 'title'))(element)} {attribute_values(element)}".strip(),
    "hybrid: words + ancestor path": lambda element: f"{compose(('name', 'url', 'title'))(element)} {element.path}".strip(),
}


@dataclass(frozen=True)
class SweepOutcome:
    """One cell of the grid: a model, an indexed text, and how they did on one surface."""

    model_name: str
    text_name: str
    surface_name: str
    accuracy_by_family: dict[str, float]
    recall_by_depth: dict[int, float]
    mean_reciprocal_rank: float
    hit_by_query: numpy.ndarray
    empty_key_fraction: float


def _unit_rows(matrix) -> numpy.ndarray:
    vectors = numpy.asarray(matrix, dtype=numpy.float32)
    return vectors / numpy.maximum(numpy.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


def evaluate_cell(model: StaticModel, model_name: str, text_name: str, build_text,
                  corpora: list[Corpus], queries, recall_depths=DEFAULT_RECALL_DEPTHS) -> SweepOutcome:
    """Rank every corpus of one surface with one (model, indexed text) pair."""
    ranks_by_family: dict[str, list[int | None]] = {}
    hits: list[float] = []
    empty_keys = 0
    total_elements = 0
    for corpus in corpora:
        texts = [build_text(element) for element in corpus.elements]
        empty_keys += sum(1 for text in texts if not text.strip())
        total_elements += len(texts)
        document_vectors = _unit_rows(model.encode([text or " " for text in texts],
                                                   show_progress_bar=False))
        for family_name, family_queries in queries[corpus.site_name].items():
            if not family_queries:
                continue
            query_vectors = _unit_rows(model.encode([query.text for query in family_queries],
                                                    show_progress_bar=False))
            similarities = query_vectors @ document_vectors.T
            order = numpy.argsort(-similarities, axis=1, kind="stable")
            for row, query in zip(order, family_queries):
                position = int(numpy.where(row == query.target_index)[0][0])
                ranks_by_family.setdefault(family_name, []).append(position)
                hits.append(float(position == 0))

    all_ranks = [rank for ranks in ranks_by_family.values() for rank in ranks]
    return SweepOutcome(
        model_name=model_name,
        text_name=text_name,
        surface_name=corpora[0].surface_name,
        accuracy_by_family={
            family: sum(1 for rank in ranks if rank == 0) / len(ranks)
            for family, ranks in ranks_by_family.items()
        },
        recall_by_depth={
            depth: sum(1 for rank in all_ranks if rank < depth) / len(all_ranks)
            for depth in recall_depths
        },
        mean_reciprocal_rank=float(numpy.mean([1.0 / (rank + 1) for rank in all_ranks])),
        hit_by_query=numpy.array(hits),
        empty_key_fraction=empty_keys / total_elements if total_elements else 0.0,
    )


def sweep_surface(surface_name: str) -> list[SweepOutcome]:
    """Every model against every indexed text, on one surface."""
    corpora = load_all_corpora(surface_name=surface_name)
    if not corpora:
        logger.warning("no %s corpora recorded", surface_name)
        return []
    queries = build_queries(corpora)
    outcomes = []
    for model_name, repository in EMBEDDING_MODELS.items():
        model = StaticModel.from_pretrained(repository)
        for text_name, build_text in INDEXED_TEXTS.items():
            outcomes.append(evaluate_cell(model, model_name, text_name, build_text, corpora, queries))
        del model
    return outcomes


def only_declared(corpora: list[Corpus]) -> list[Corpus]:
    """The same corpora, keeping only elements whose surface actually declared them."""
    filtered = []
    for corpus in corpora:
        elements = tuple(element for element in corpus.elements if element.source.strip())
        if len(elements) >= 30:
            filtered.append(Corpus(site_name=corpus.site_name, surface_name=corpus.surface_name,
                                   page_url=corpus.page_url, elements=elements))
    return filtered


def sweep_declared_only(surface_name: str, model_names: tuple[str, ...],
                        text_names: tuple[str, ...]) -> list[SweepOutcome]:
    """The coverage-matched comparison: every element in play has a declaration."""
    corpora = only_declared(load_all_corpora(surface_name=surface_name))
    if not corpora:
        return []
    queries = build_queries(corpora)
    outcomes = []
    for model_name in model_names:
        model = StaticModel.from_pretrained(EMBEDDING_MODELS[model_name])
        for text_name in text_names:
            outcomes.append(evaluate_cell(model, model_name, text_name, INDEXED_TEXTS[text_name],
                                          corpora, queries))
        del model
    return outcomes


def as_frame(outcomes: list[SweepOutcome]) -> pandas.DataFrame:
    rows = []
    for outcome in outcomes:
        rows.append({
            "surface": outcome.surface_name,
            "model": outcome.model_name,
            "indexed_text": outcome.text_name,
            **{f"{family}@1": value for family, value in outcome.accuracy_by_family.items()},
            **{f"recall@{depth}": value for depth, value in outcome.recall_by_depth.items()},
            "MRR": outcome.mean_reciprocal_rank,
            "empty_keys": outcome.empty_key_fraction,
        })
    return pandas.DataFrame(rows)


def main() -> int:
    pandas.set_option("display.width", 220)
    pandas.set_option("display.max_columns", 30)
    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)

    everything: list[SweepOutcome] = []
    for surface_name in ("web", "native"):
        outcomes = sweep_surface(surface_name)
        everything.extend(outcomes)
        if not outcomes:
            continue
        frame = as_frame(outcomes).sort_values("MRR", ascending=False)
        logger.info("\n===== %s: %d models x %d indexed texts =====\n%s", surface_name,
                    len(EMBEDDING_MODELS), len(INDEXED_TEXTS),
                    frame.round(3).to_string(index=False))

        reference = next((outcome for outcome in outcomes
                          if outcome.model_name == "M2V_multilingual (live)"
                          and outcome.text_name.startswith("words:")
                          and (surface_name == "web") == ("web" in outcome.text_name)), None)
        if reference is None:
            continue
        comparisons = []
        for outcome in outcomes:
            if outcome is reference:
                continue
            interval = paired_bootstrap_interval(outcome.hit_by_query, reference.hit_by_query)
            difference, low, high = interval
            comparisons.append({
                "model": outcome.model_name, "indexed_text": outcome.text_name,
                "difference": f"{difference:+.1%}", "interval": f"[{low:+.1%}, {high:+.1%}]",
                "verdict": "separable" if is_separable(interval) else "indistinguishable",
            })
        logger.info("\n%s: paired against what ships (%s / %s)\n%s", surface_name,
                    reference.model_name, reference.text_name,
                    pandas.DataFrame(comparisons).to_string(index=False))

    frame = as_frame(everything)
    destination = RESULTS_DIRECTORY / "model_sweep.parquet"
    try:
        frame.to_parquet(destination)
    except Exception:  # noqa: BLE001 — parquet needs an engine installed; csv always works
        destination = destination.with_suffix(".csv")
        frame.to_csv(destination, index=False)
    logger.info("saved %d cells to %s", len(frame), destination)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
