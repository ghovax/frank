"""Per-source adapters and record normalizers for the six discovery backends
(OpenAlex, Crossref, arXiv, PubMed, Europe PMC, Semantic Scholar)."""
from typing import Any

import arxiv
from Bio import Entrez
from habanero import Crossref
from pyalex import Works
from semanticscholar import SemanticScholar

from .common import (
    OPENALEX_TYPE_ALIASES,
    _attr_or_key,
    _blank_record,
    _http_get,
    _reconstruct_abstract,
    _strip_doi,
    _within_year_range,
)


def _normalize_openalex(work: dict, position: int) -> dict:
    """Map an OpenAlex work (pyalex Work) into the common record shape."""
    record = _blank_record("openalex", position)
    record["title"] = work.get("title")
    record["authors"] = [authorship["author"]["display_name"] for authorship in work.get("authorships", [])]
    record["year"] = work.get("publication_year")
    record["venue"] = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    record["abstract"] = _reconstruct_abstract(work.get("abstract_inverted_index"))
    record["citations"] = work.get("cited_by_count", 0)
    record["fwci"] = work.get("fwci") or 0.0
    doi = _strip_doi(work.get("doi"))
    record["ids"] = {"doi": doi, "openalex": work.get("id"),
                     "pmid": (work.get("ids") or {}).get("pmid"), "pmcid": (work.get("ids") or {}).get("pmcid")}
    open_access = work.get("open_access") or {}
    best_location = work.get("best_oa_location") or {}
    record["oa"] = {"is_oa": open_access.get("is_oa"), "status": open_access.get("oa_status"),
                    "pdf_url": best_location.get("pdf_url") or open_access.get("oa_url")}
    record["url"] = f"https://doi.org/{doi}" if doi else None
    return record


def _normalize_crossref(item: dict, position: int) -> dict:
    """Map a Crossref item into the common record shape."""
    record = _blank_record("crossref", position)
    record["title"] = (item.get("title") or [None])[0]
    record["authors"] = [
        f"{author.get('family', '')}, {author.get('given', '')}".strip(", ") for author in item.get("author", [])
    ]
    record["year"] = (item.get("issued", {}).get("date-parts", [[None]]) or [[None]])[0][0]
    record["venue"] = (item.get("container-title") or [None])[0]
    record["citations"] = item.get("is-referenced-by-count", 0)
    record["ids"] = {"doi": _strip_doi(item.get("DOI"))}
    record["url"] = item.get("URL")
    return record


def _normalize_arxiv(result, position: int) -> dict:
    """Map an arxiv.Result object into the common record shape."""
    record = _blank_record("arxiv", position)
    record["title"] = (result.title or "").strip()
    record["authors"] = [author.name for author in result.authors]
    record["year"] = result.published.year if result.published else None
    record["abstract"] = (result.summary or "").strip()
    arxiv_id = result.get_short_id()
    record["ids"] = {"arxiv": arxiv_id, "doi": _strip_doi(result.doi)}
    record["oa"] = {"is_oa": True, "status": "green", "pdf_url": result.pdf_url}
    record["url"] = result.entry_id
    return record


def _normalize_pubmed(summary, position: int) -> dict:
    """Map a PubMed esummary docsum (Bio.Entrez) into the common record shape."""
    record = _blank_record("pubmed", position)
    record["title"] = str(summary["Title"]) if summary.get("Title") else None
    record["authors"] = [str(author) for author in summary.get("AuthorList", [])]
    pubdate = str(summary.get("PubDate", ""))
    record["year"] = int(pubdate[:4]) if pubdate[:4].isdigit() else None
    record["venue"] = str(summary.get("Source")) if summary.get("Source") else None
    doi = summary.get("DOI")
    record["ids"] = {"pmid": str(summary.get("Id")), "doi": _strip_doi(str(doi)) if doi else None}
    return record


def _normalize_europepmc(result: dict, position: int) -> dict:
    """Map a Europe PMC result into the common record shape."""
    record = _blank_record("europepmc", position)
    record["title"] = result.get("title")
    record["authors"] = (result.get("authorString") or "").split(", ") if result.get("authorString") else []
    record["year"] = int(result["pubYear"]) if str(result.get("pubYear", "")).isdigit() else None
    record["venue"] = (result.get("journalInfo") or {}).get("journal", {}).get("title")
    record["abstract"] = result.get("abstractText")
    record["citations"] = result.get("citedByCount", 0)
    record["ids"] = {"doi": _strip_doi(result.get("doi")), "pmid": result.get("pmid"),
                     "pmcid": result.get("pmcid")}
    record["oa"] = {"is_oa": result.get("isOpenAccess") == "Y"}
    return record


def _normalize_semanticscholar(paper, position: int) -> dict:
    """Map a Semantic Scholar Paper into the common record shape."""
    record = _blank_record("semanticscholar", position)
    record["title"] = paper.title
    record["authors"] = [author.name for author in (paper.authors or [])]
    record["year"] = paper.year
    record["venue"] = paper.venue
    record["abstract"] = paper.abstract or _attr_or_key(paper.tldr, "text")
    record["citations"] = paper.citationCount or 0
    external = paper.externalIds or {}
    record["ids"] = {"doi": _strip_doi(external.get("DOI")), "arxiv": external.get("ArXiv"),
                     "pmid": external.get("PubMed"), "s2": getattr(paper, "paperId", None)}
    record["oa"] = {"pdf_url": _attr_or_key(paper.openAccessPdf, "url")}
    return record


def _source_openalex(options: dict) -> list[dict]:
    """Query OpenAlex through pyalex and return normalized records."""
    query = Works().search(options["query"])
    if options.get("type"):
        query = query.filter(type=OPENALEX_TYPE_ALIASES.get(options["type"], options["type"]))
    if options.get("from_year"):
        query = query.filter(from_publication_date=f"{options['from_year']}-01-01")
    if options.get("to_year"):
        query = query.filter(to_publication_date=f"{options['to_year']}-12-31")
    if options.get("open_access"):
        query = query.filter(is_oa=True)
    sort_field = {"citations": "cited_by_count", "date": "publication_date"}.get(options.get("sort") or "")
    if sort_field:
        query = query.sort(**{sort_field: "desc"})
    works: Any = query.get(per_page=options["limit"])
    return [_normalize_openalex(work, position) for position, work in enumerate(works)]


def _source_crossref(options: dict) -> list[dict]:
    """Query Crossref through habanero and return normalized records."""
    crossref_filter: dict[str, str] = {}
    if options.get("type"):
        crossref_filter["type"] = options["type"]
    if options.get("from_year"):
        crossref_filter["from_pub_date"] = f"{options['from_year']}-01-01"
    if options.get("to_year"):
        crossref_filter["until_pub_date"] = f"{options['to_year']}-12-31"
    keyword_arguments: dict[str, Any] = {"query_bibliographic": options["query"], "limit": options["limit"]}
    if crossref_filter:
        keyword_arguments["filter"] = crossref_filter
    sort_field = {"citations": "is-referenced-by-count", "date": "published"}.get(options.get("sort") or "")
    if sort_field:
        keyword_arguments["sort"] = sort_field
        keyword_arguments["order"] = "desc"
    payload: Any = Crossref().works(**keyword_arguments)
    return [_normalize_crossref(item, position) for position, item in enumerate(payload["message"]["items"])]


def _source_arxiv(options: dict) -> list[dict]:
    """Query arXiv through the arxiv client and return normalized records, applying the year window client-side."""
    sort_by = arxiv.SortCriterion.SubmittedDate if options.get("sort") == "date" else arxiv.SortCriterion.Relevance
    search = arxiv.Search(query="all:" + options["query"], max_results=options["limit"],
                          sort_by=sort_by, sort_order=arxiv.SortOrder.Descending)
    records = []
    for position, result in enumerate(arxiv.Client().results(search)):
        record = _normalize_arxiv(result, position)
        if _within_year_range(record["year"], options):
            records.append(record)
    return records


def _source_pubmed(options: dict) -> list[dict]:
    """Query PubMed through Bio.Entrez (esearch then esummary) and return normalized records."""
    term = options["query"]
    if options.get("from_year") and options.get("to_year"):
        term += f" AND {options['from_year']}:{options['to_year']}[dp]"
    sort = "pub_date" if options.get("sort") == "date" else "relevance"
    with Entrez.esearch(db="pubmed", term=term, retmax=options["limit"], sort=sort) as handle:
        search_result: Any = Entrez.read(handle)
    id_list = search_result["IdList"]
    if not id_list:
        return []
    with Entrez.esummary(db="pubmed", id=",".join(id_list)) as handle:
        summaries: Any = Entrez.read(handle)
    return [_normalize_pubmed(summary, position) for position, summary in enumerate(summaries)]


def _source_europepmc(options: dict) -> list[dict]:
    """Query Europe PMC over its REST API and return normalized records."""
    query = options["query"]
    if options.get("from_year") and options.get("to_year"):
        query += f" AND PUB_YEAR:[{options['from_year']} TO {options['to_year']}]"
    if options.get("open_access"):
        query += " AND OPEN_ACCESS:Y"
    params = {"query": query, "format": "json", "resultType": "core", "pageSize": options["limit"]}
    payload = _http_get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", params).json()
    return [_normalize_europepmc(result, position) for position, result in enumerate(payload["resultList"]["result"])]


def _source_semanticscholar(options: dict) -> list[dict]:
    """Query Semantic Scholar and return normalized records.

    retry=True lets the client back off through anonymous-pool 429s rather than being skipped;
    iteration stops at the requested limit so the paginator does not fetch further pages.
    """
    fields = ["title", "year", "venue", "authors", "abstract", "citationCount",
              "externalIds", "openAccessPdf", "tldr", "paperId"]
    keyword_arguments: dict[str, Any] = {"query": options["query"], "limit": min(options["limit"], 100), "fields": fields}
    if options.get("from_year") and options.get("to_year"):
        keyword_arguments["year"] = f"{options['from_year']}-{options['to_year']}"
    api_key = options.get("s2_api_key")
    scholar = (SemanticScholar(api_key=api_key, timeout=30, retry=True) if api_key
               else SemanticScholar(timeout=30, retry=True))
    results: Any = scholar.search_paper(**keyword_arguments)
    records = []
    for position, paper in enumerate(results):
        records.append(_normalize_semanticscholar(paper, position))
        if len(records) >= options["limit"]:
            break
    return records


SOURCE_ADAPTERS = {
    "openalex": _source_openalex,
    "crossref": _source_crossref,
    "arxiv": _source_arxiv,
    "pubmed": _source_pubmed,
    "europepmc": _source_europepmc,
    "semanticscholar": _source_semanticscholar,
}
