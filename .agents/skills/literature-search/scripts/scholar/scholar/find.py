"""Layer 1 discovery: fan out a query across every source in parallel, then merge
and rank the results."""
import concurrent.futures
import os

import pyalex
from Bio import Entrez

from .common import DEFAULT_SOURCES, PER_RUN_TIMEOUT, batchable, logger
from .ranking import merge_records, order_results, warn_search_filters
from .sources import SOURCE_ADAPTERS


@batchable("query")
def search(query, *, sources=None, limit=20, from_year=None, to_year=None,
           open_access=None, work_type=None, sort=None, email=None, s2_api_key=None) -> dict:
    """Layer 1 — topical discovery across all sources in parallel, de-duplicated and ranked.

    Search broad by default: leave work_type, open_access, and the year window unset unless the
    user asks to narrow, so preprints, reviews, and articles are all in scope. Returns
    {"meta": {...}, "results": [...]}, and logs every narrowing filter as a warning (logger.warning).
    """
    options = {
        "query": query, "limit": limit, "from_year": from_year, "to_year": to_year,
        "open_access": open_access, "type": work_type, "sort": sort,
        "email": email or "",
        "s2_api_key": s2_api_key if s2_api_key is not None else os.environ.get("S2_API_KEY"),
    }
    pyalex.config.email = options["email"] or None
    Entrez.email = options["email"] or None
    selected = [name for name in (sources or DEFAULT_SOURCES) if name in SOURCE_ADAPTERS]
    logger.info("search %r across %s (limit=%s)", query, selected, limit)

    collected: list[dict] = []
    source_meta: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {pool.submit(SOURCE_ADAPTERS[name], options): name for name in selected}
        done, not_done = concurrent.futures.wait(futures, timeout=PER_RUN_TIMEOUT)
        for future in done:
            name = futures[future]
            try:
                records = future.result()
                collected.extend(records)
                source_meta[name] = f"ok ({len(records)} records)"
                logger.info("%s returned %d records", name, len(records))
            except Exception as error:  # noqa: BLE001
                source_meta[name] = f"failed: {type(error).__name__}: {error}"
                logger.warning("%s failed: %s: %s", name, type(error).__name__, error)
        for future in not_done:
            name = futures[future]
            source_meta[name] = f"failed: timed out after {PER_RUN_TIMEOUT:.0f}s"
            logger.warning("%s timed out after %.0fs", name, PER_RUN_TIMEOUT)

    merged = merge_records(collected)
    ordered = order_results(merged, options)
    warn_search_filters(options, selected, source_meta)
    return {"meta": {"operation": "search", "sources": source_meta,
                     "count": len(ordered), "raw_records": len(collected)},
            "results": ordered}
