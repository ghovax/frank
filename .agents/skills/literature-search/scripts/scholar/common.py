"""Foundation shared across the scholar package: constants, logging, HTTP, .env
loading, record helpers, and the batch fan-out infrastructure."""
import concurrent.futures
import functools
import logging
import os
import pathlib
import re
import sys
import tempfile

import httpx


DEFAULT_SOURCES = ["openalex", "crossref", "arxiv", "pubmed", "europepmc", "semanticscholar"]
REQUEST_TIMEOUT = httpx.Timeout(30.0)
PER_RUN_TIMEOUT = 45.0  # A source still running after this is recorded as timed out, not awaited forever.
DEFAULT_LOG_PATH = os.path.join(tempfile.gettempdir(), "scholar.log")

RANK_WEIGHTS = {
    "lexical_relevance": 1.0,
    "citation_impact": 0.8,
    "recency": 0.6,
    "cross_source_agreement": 1.2,
    "venue_authority": 0.5,
}

OPENALEX_TYPE_ALIASES = {"journal-article": "article"}  # OpenAlex names this type "article".
ARXIV_ID_PATTERN = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
FACET_FIELDS = {
    "year": "publication_year",
    "institution": "authorships.institutions.id",
    "venue": "primary_location.source.id",
    "type": "type",
    "open_access": "open_access.oa_status",
    "topic": "primary_topic.id",
    "country": "authorships.countries",
}


logger = logging.getLogger("scholar")


def configure_logging(log_path: str = DEFAULT_LOG_PATH) -> None:
    """Optional: route this library's INFO logs to standard error and a flushed log file.

    Call once from the importing process to capture a diagnostic run log. If never called, the
    library stays quiet, because its logger has no handlers.
    """
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, mode="w")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.handlers = [file_handler, stream_handler]


def _http_get(url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
    """Issue a redirect-following GET with the project user agent and shared timeout."""
    merged = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", **(headers or {})}
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=merged) as client:
        return client.get(url, params=params)


def _blank_record(source: str, position: int) -> dict:
    """Return an empty normalized record tagged with the source that produced it and its rank there."""
    return {
        "title": None, "authors": [], "year": None, "venue": None, "abstract": None,
        "citations": 0, "fwci": 0.0, "ids": {}, "oa": {}, "url": None,
        "found_by": [source], "source_rank": position,
    }


def _attr_or_key(obj, name: str):
    """Read a field that may be exposed either as an object attribute or a dict key."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _strip_doi(value: str | None) -> str | None:
    """Return a bare, lowercased DOI with any URL or doi: prefix removed, or None."""
    if not value:
        return None
    return value.lower().replace("https://doi.org/", "").replace("doi:", "").strip()


def _within_year_range(year: int | None, options: dict) -> bool:
    """Report whether a publication year falls inside the optional year window."""
    if year is None:
        return True
    if options.get("from_year") and year < options["from_year"]:
        return False
    if options.get("to_year") and year > options["to_year"]:
        return False
    return True


def _reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Rebuild the plain-text abstract from OpenAlex's word-to-positions inverted index."""
    if not inverted_index:
        return None
    positioned_words = sorted(
        (position, word) for word, positions in inverted_index.items() for position in positions
    )
    return " ".join(word for _, word in positioned_words)


def _normalized_title(paper: dict) -> str:
    """Return a lowercase, alphanumeric-only title, used as a secondary identity key."""
    return "".join(character for character in (paper.get("title") or "").lower() if character.isalnum())


_DOTENV_LOADED = False


def _load_dotenv() -> None:
    """Populate os.environ from the first .env found in the working dir or above the script (once)."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    candidates = [pathlib.Path.cwd() / ".env"]
    candidates += [parent / ".env" for parent in pathlib.Path(__file__).resolve().parents]
    for env_path in candidates:
        try:
            if not env_path.is_file():
                continue
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))
            break
        except Exception:  # noqa: BLE001 - a malformed .env should not crash the import path
            continue
    _DOTENV_LOADED = True


MAXIMUM_BATCH_WORKERS = 8  # Cap fan-out width so a big list does not stampede the upstream APIs.


def _parallel_map(func, items: list, maximum_workers: int = MAXIMUM_BATCH_WORKERS) -> list:
    """Run `func` over `items` concurrently, returning results in the original order.

    `func` is expected to swallow its own exceptions (the batch wrappers below pass a closure that
    turns any error into an `{"_error": ...}` record), so this helper never raises for one bad item.
    """
    if not items:
        return []
    results: list = [None] * len(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(maximum_workers, len(items))) as pool:
        futures = {pool.submit(func, item): index for index, item in enumerate(items)}
        for future in concurrent.futures.as_completed(futures):
            results[futures[future]] = future.result()
    return results


def _batch_summary(operation: str, results: list) -> dict:
    """Wrap a list of per-item results with batch metadata (count, ok, failed)."""
    ok = sum(1 for record in results if not (isinstance(record, dict) and record.get("_error")))
    return {
        "meta": {"operation": operation, "batch": True, "count": len(results),
                 "ok": ok, "failed": len(results) - ok},
        "results": results,
    }


def batchable(primary: str):
    """Decorator: if the function's `primary` argument is a list/tuple, fan out over it in parallel.

    The decorated function still returns its single result dict for a scalar `primary`. For a list it
    returns {"meta": {batch metadata}, "results": [<per-item result or {"_error": ...}>, ...]} in input
    order. `primary` is passed to the wrapped function as its first positional argument for each item.
    """
    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if args:
                key, rest = args[0], args[1:]
            elif primary in kwargs:
                key, rest = kwargs[primary], ()
            else:
                return func(*args, **kwargs)  # Let the wrapped function raise its own arity error.
            if not isinstance(key, (list, tuple)):
                return func(*args, **kwargs)
            shared = {name: value for name, value in kwargs.items() if name != primary}

            def call_one(item):
                try:
                    return func(item, *rest, **shared)
                except Exception as error:  # noqa: BLE001 - one bad item must not sink the batch
                    return {"_error": f"{type(error).__name__}: {error}", "input": item}

            return _batch_summary(func.__name__, _parallel_map(call_one, list(key)))
        return wrapper
    return decorate
