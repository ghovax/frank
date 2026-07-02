"""Cross-source de-duplication (union-find) and the composite ranking used to
order merged search results."""
from collections import defaultdict

import numpy as np

from .common import DEFAULT_SOURCES, RANK_WEIGHTS, _normalized_title, logger


def merge_records(records: list[dict]) -> list[dict]:
    """Fuse records that refer to the same paper.

    Two records are treated as the same work when they share a DOI or an identical normalized
    title. A union-find pass connects both kinds of match, so a paper carrying different DOIs in
    different sources (for example a preprint and its published version) is still merged into one
    record. Normalized titles shorter than ten characters are ignored, being too generic to match.
    """
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        parent[find(first)] = find(second)

    first_seen_by_doi: dict[str, int] = {}
    first_seen_by_title: dict[str, int] = {}
    for index, record in enumerate(records):
        doi = (record.get("ids") or {}).get("doi")
        if doi:
            union(index, first_seen_by_doi.setdefault(doi, index))
        title = _normalized_title(record)
        if len(title) >= 10:
            union(index, first_seen_by_title.setdefault(title, index))

    groups: dict[int, list[dict]] = defaultdict(list)
    for index, record in enumerate(records):
        groups[find(index)].append(record)
    return [_fuse_group(group) for group in groups.values()]


def _fuse_group(group: list[dict]) -> dict:
    """Collapse a group of duplicate records into one, keeping the most complete value for each field."""
    fused_ids = {key: value for record in group for key, value in record.get("ids", {}).items() if value}
    fused_oa = {key: value for record in group for key, value in (record.get("oa") or {}).items() if value}
    return {
        "title": next((record["title"] for record in group if record.get("title")), None),
        "authors": max((record.get("authors") or [] for record in group), key=len),
        "year": min((record["year"] for record in group if record.get("year")), default=None),
        "venue": next((record["venue"] for record in group if record.get("venue")), None),
        "abstract": max((record.get("abstract") or "" for record in group), key=len) or None,
        "citations": max((record.get("citations", 0) for record in group), default=0),
        "fwci": max((record.get("fwci", 0.0) for record in group), default=0.0),
        "source_rank": min((record.get("source_rank", 999) for record in group), default=999),
        "ids": fused_ids,
        "oa": fused_oa,
        "url": next((record["url"] for record in group if record.get("url")), None),
        "found_by": sorted({source for record in group for source in record.get("found_by", [])}),
    }


def _normalize_min_max(values: np.ndarray) -> np.ndarray:
    """Scale an array into the 0..1 range; a flat array (no spread) maps to all zeros."""
    spread = values.max() - values.min()
    return np.zeros_like(values) if spread == 0 else (values - values.min()) / spread


def score_papers(papers: list[dict], current_year: int) -> np.ndarray:
    """Compute a weighted composite score for every paper, one matrix row per ranking signal."""
    signal_matrix = np.vstack([
        _normalize_min_max(np.array([1.0 / (paper.get("source_rank", 999) + 1) for paper in papers])),
        _normalize_min_max(np.log1p([paper.get("citations", 0) for paper in papers])),
        _normalize_min_max(np.array([paper.get("year") or current_year for paper in papers], dtype=float)),
        _normalize_min_max(np.array([len(paper.get("found_by", [])) for paper in papers], dtype=float)),
        _normalize_min_max(np.array([paper.get("fwci", 0.0) for paper in papers])),
    ])
    return np.array(list(RANK_WEIGHTS.values())) @ signal_matrix


def order_results(papers: list[dict], options: dict) -> list[dict]:
    """Order merged papers by an explicit citations/date sort, or by the composite score."""
    if not papers:
        return []
    if options.get("sort") == "citations":
        return sorted(papers, key=lambda paper: paper.get("citations", 0), reverse=True)
    if options.get("sort") == "date":
        return sorted(papers, key=lambda paper: paper.get("year") or 0, reverse=True)
    current_year = (options.get("to_year") or options.get("from_year") or 2026)
    scores = score_papers(papers, current_year)
    return [paper for paper, _ in sorted(zip(papers, scores), key=lambda pair: pair[1], reverse=True)]


def warn_search_filters(options: dict, queried_sources: list[str], source_meta: dict[str, str]) -> None:
    """Log every narrowing decision that could have excluded papers, so the caller can relay it."""
    if options.get("open_access"):
        logger.warning(
            "Restricted to OPEN-ACCESS papers (OpenAlex and Europe PMC); any work without an "
            "open-access copy was excluded. Set open_access=False to include all papers."
        )
    if options.get("type"):
        logger.warning(
            "Restricted to publication type '%s' (OpenAlex and Crossref); other publication types were excluded.",
            options["type"],
        )
    if options.get("from_year") or options.get("to_year"):
        logger.warning(
            "Restricted to publication years %s-%s; papers outside this window were excluded.",
            options.get("from_year", "any"), options.get("to_year", "any"),
        )
    logger.warning(
        "Each source returned at most %s results; more matching papers very likely exist beyond this "
        "per-source cap. Raise the limit to widen coverage.", options["limit"],
    )
    not_consulted = [source for source in DEFAULT_SOURCES if source not in queried_sources]
    if not_consulted:
        logger.warning("Databases NOT consulted in this run: %s.", ", ".join(not_consulted))
    failed = [name for name, status in source_meta.items() if not status.startswith("ok")]
    if failed:
        logger.warning("Returned no results because the query failed or was rate-limited: %s.", ", ".join(failed))
