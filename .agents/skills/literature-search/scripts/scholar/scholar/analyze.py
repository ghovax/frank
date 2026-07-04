"""Layer 2 analysis: id resolution, citation graph, similar work, facets, and
author disambiguation / co-authors / works, plus the Semantic Scholar helpers."""
import os
import time
import urllib.parse
from collections import Counter
from typing import Any

import arxiv
import pyalex
from pyalex import Authors, Works

from .common import (
    ARXIV_ID_PATTERN,
    FACET_FIELDS,
    _blank_record,
    _http_get,
    _strip_doi,
    batchable,
    logger,
)
from .ranking import order_results
from .sources import _normalize_arxiv, _normalize_openalex


def _fetch_arxiv_record(arxiv_id: str) -> dict:
    """Fetch a single arXiv paper by id and return it as a normalized record."""
    clean_id = arxiv_id.lower().replace("arxiv:", "").strip()
    result = next(arxiv.Client().results(arxiv.Search(id_list=[clean_id])))
    return _normalize_arxiv(result, 0)


def _resolve_work(identifier: str) -> tuple[Any, dict]:
    """Resolve a paper id to (OpenAlex work or None, normalized record).

    OpenAlex resolves DOIs, PMIDs, PMCIDs, and its own W-ids; arXiv ids (and arXiv DOIs, which
    OpenAlex indexes unreliably) are fetched through the arxiv client instead. The OpenAlex work
    is returned alongside the record because the graph operations need its referenced_works and
    related_works fields; it is None for the arXiv-only path.
    """
    identifier = identifier.strip()
    if ARXIV_ID_PATTERN.match(identifier) or identifier.lower().startswith("arxiv:"):
        return None, _fetch_arxiv_record(identifier)
    work: Any
    if identifier.upper().startswith("W") and identifier[1:].isdigit():
        work = Works()[identifier]
    elif identifier.lower().startswith("pmid:") or identifier.isdigit():
        work = Works()[f"pmid:{identifier.split(':')[-1]}"]
    elif identifier.lower().startswith("pmc"):
        work = Works()[f"pmcid:{identifier}"]
    else:
        doi = identifier.lower().split("doi.org/")[-1].replace("doi:", "")
        if doi.startswith("10.48550/arxiv"):
            return None, _fetch_arxiv_record(doi.split("arxiv.")[-1])
        work = Works()[f"https://doi.org/{doi}"]
    return work, _normalize_openalex(work, 0)


def _openalex_works_by_ids(openalex_ids: list[str]) -> list[Any]:
    """Fetch several OpenAlex works by id, batching to stay within filter-length limits."""
    fetched: list[Any] = []
    for start in range(0, len(openalex_ids), 50):
        chunk = openalex_ids[start:start + 50]
        results: Any = Works().filter(openalex_id="|".join(chunk)).get(per_page=len(chunk))
        fetched.extend(results)
    return fetched


def _openalex_authors_by_ids(author_ids: list[str]) -> dict[str, Any]:
    """Fetch several OpenAlex authors by id and return them keyed by their full OpenAlex id."""
    by_id: dict[str, Any] = {}
    for start in range(0, len(author_ids), 50):
        chunk = author_ids[start:start + 50]
        results: Any = Authors().filter(openalex_id="|".join(chunk)).get(per_page=len(chunk))
        for author in results:
            by_id[author["id"]] = author
    return by_id


def _author_summary(author: dict, openalex_id: str) -> dict:
    """Shape an OpenAlex author into the common summary used by find_authors and coauthors."""
    return {
        "name": author.get("display_name"),
        "openalex_id": openalex_id.split("/")[-1],
        "orcid": author.get("orcid"),
        "works_count": author.get("works_count"),
        "h_index": (author.get("summary_stats") or {}).get("h_index"),
        "cited_by_count": author.get("cited_by_count"),
        "affiliations": [inst.get("display_name") for inst in (author.get("last_known_institutions") or [])],
        "topics": [topic.get("display_name") for topic in (author.get("topics") or [])[:5]],
    }


def _resolve_openalex_author(identifier: str) -> Any:
    """Resolve an OpenAlex author by its id (an "A..." string) or by a free-text name search."""
    if identifier.upper().startswith("A") and identifier[1:].isdigit():
        try:
            return Authors()[identifier]
        except Exception:  # noqa: BLE001 - fall back to a name search if the id lookup fails
            pass
    matches: Any = Authors().search(identifier).get(per_page=1)
    if not matches:
        raise ValueError(f"No OpenAlex author matched {identifier!r}")
    return matches[0]


def _author_email(email) -> None:
    """Set the module-level pyalex contact email from an explicit value or the environment."""
    pyalex.config.email = email or None


def _s2_id(identifier: str) -> str:
    """Turn a paper id into the form the Semantic Scholar client expects (DOI:..., ARXIV:..., or as-is)."""
    ident = identifier.strip()
    low = ident.lower()
    if low.startswith("10.") or "doi.org/" in low:
        return "DOI:" + low.split("doi.org/")[-1].replace("doi:", "")
    if ARXIV_ID_PATTERN.match(ident):
        return "ARXIV:" + ident
    return ident


def _s2_get(url: str, params: dict) -> dict:
    """GET a Semantic Scholar Graph endpoint (id pre-encoded in the path), retrying briefly through 429s.

    The official client does not URL-encode ids, so DOIs (which contain '/') break its path; calling the
    REST API directly with an encoded id avoids that.
    """
    headers = {"x-api-key": os.environ["S2_API_KEY"]} if os.environ.get("S2_API_KEY") else {}
    for attempt in range(4):
        response = _http_get(url, params, headers)
        if response.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("Semantic Scholar rate-limited; set S2_API_KEY for reliable access.")


def _normalize_s2json(paper: dict, position: int) -> dict:
    """Map a raw Semantic Scholar Graph paper (JSON dict) into the common record shape."""
    record = _blank_record("semanticscholar", position)
    record["title"] = paper.get("title")
    record["authors"] = [author.get("name") for author in (paper.get("authors") or [])]
    record["year"] = paper.get("year")
    record["venue"] = paper.get("venue")
    record["abstract"] = paper.get("abstract")
    record["citations"] = paper.get("citationCount") or 0
    external = paper.get("externalIds") or {}
    record["ids"] = {"doi": _strip_doi(external.get("DOI")), "arxiv": external.get("ArXiv"),
                     "pmid": external.get("PubMed"), "s2": paper.get("paperId")}
    record["oa"] = {"pdf_url": (paper.get("openAccessPdf") or {}).get("url")}
    return record


def _citations_via_s2(paper_id: str, direction: str, limit: int) -> dict:
    """Citation graph via the Semantic Scholar Graph API, with influential-citation flags and contexts."""
    endpoint = "references" if direction == "references" else "citations"
    encoded = urllib.parse.quote(_s2_id(paper_id), safe=":")
    fields = "contexts,isInfluential,title,year,authors,abstract,venue,citationCount,externalIds,openAccessPdf"
    data = _s2_get(f"https://api.semanticscholar.org/graph/v1/paper/{encoded}/{endpoint}",
                   {"fields": fields, "limit": min(limit, 1000)})
    records = []
    for position, row in enumerate(data.get("data", [])):
        paper = row.get("citingPaper") or row.get("citedPaper") or {}
        record = _normalize_s2json(paper, position)
        record["is_influential"] = row.get("isInfluential")
        record["contexts"] = row.get("contexts")
        records.append(record)
    logger.warning(
        "%s from Semantic Scholar, up to %s, annotated with influential-citation flags and the "
        "sentences (contexts) where the citation occurs.",
        "References" if direction == "references" else "Citing works", limit,
    )
    return {"meta": {"operation": "citations", "direction": direction, "source": "semanticscholar",
                     "returned": len(records)},
            "paper": {"id": paper_id}, "results": records}


def _recommend_via_s2(paper_id: str, limit: int) -> dict:
    """Similar papers via the Semantic Scholar recommender."""
    encoded = urllib.parse.quote(_s2_id(paper_id), safe=":")
    fields = "title,year,authors,abstract,venue,citationCount,externalIds,openAccessPdf"
    data = _s2_get(f"https://api.semanticscholar.org/recommendations/v1/papers/forpaper/{encoded}",
                   {"fields": fields, "limit": limit})
    records = [_normalize_s2json(paper, position) for position, paper in enumerate(data.get("recommendedPapers", []))]
    logger.warning("Recommendations from Semantic Scholar's recommender; verify topical fit before relying on them.")
    return {"meta": {"operation": "similar", "source": "semanticscholar", "returned": len(records)},
            "paper": {"id": paper_id}, "results": records}


@batchable("paper_id")
def lookup(paper_id, *, email=None) -> dict:
    """Layer 2 — fetch and normalize one paper by id (DOI, arXiv id, PMID, PMCID, or OpenAlex id)."""
    _author_email(email)
    _, record = _resolve_work(paper_id)
    return {"meta": {"operation": "lookup"}, "result": record}


@batchable("paper_id")
def citations(paper_id, *, direction="citing", limit=25, source="openalex", email=None) -> dict:
    """Layer 2 — citation graph for a paper: 'citing' works (forward) or its 'references' (backward).

    source="openalex" (default) gives broad coverage. source="semanticscholar" additionally returns,
    per citing work, an is_influential flag and the citation contexts (the sentences citing the paper).
    """
    fallback_note = None
    if source in ("s2", "semanticscholar"):
        try:
            return _citations_via_s2(paper_id, direction, limit)
        except Exception as error:  # noqa: BLE001
            logger.warning("Semantic Scholar citations failed (%s); falling back to OpenAlex.", error)
            fallback_note = ("Semantic Scholar was unavailable for this id (it resolves best with an arXiv id or an "
                             "S2_API_KEY); fell back to OpenAlex, so no citation contexts or influential flags.")
    _author_email(email)
    work, _ = _resolve_work(paper_id)
    if work is None:
        raise ValueError("Citation traversal needs an OpenAlex-indexed id (DOI, PMID, or OpenAlex id).")
    work_id = work["id"].split("/")[-1]
    works: Any
    if direction == "references":
        reference_ids = [ref.split("/")[-1] for ref in (work.get("referenced_works") or [])][:limit]
        works = _openalex_works_by_ids(reference_ids)
    else:
        works = Works().filter(cites=work_id).sort(cited_by_count="desc").get(per_page=limit)
    records = [_normalize_openalex(item, position) for position, item in enumerate(works)]
    logger.warning(
        "%s OpenAlex, up to %s; OpenAlex citation coverage is broad but not exhaustive, so treat counts as "
        "a lower bound. Pass source='semanticscholar' for influential-citation flags and citation contexts.",
        "References listed by" if direction == "references" else "Citing works from", limit,
    )
    if fallback_note:
        logger.warning("%s", fallback_note)
    return {
        "meta": {"operation": "citations", "direction": direction, "source": "openalex",
                 "returned": len(records)},
        "paper": {"openalex_id": work_id, "title": work.get("title")},
        "results": records,
    }


@batchable("paper_id")
def similar(paper_id, *, limit=10, source="openalex", email=None) -> dict:
    """Layer 2 — papers similar to a given one.

    source="openalex" (default) returns OpenAlex's related works; source="semanticscholar" returns the
    Semantic Scholar recommender's suggestions. Either way, verify topical fit before relying on them.
    """
    fallback_note = None
    if source in ("s2", "semanticscholar"):
        try:
            recommended = _recommend_via_s2(paper_id, limit)
            if recommended["meta"]["returned"]:
                return recommended
            fallback_note = "Semantic Scholar returned no recommendations; fell back to OpenAlex related works."
        except Exception as error:  # noqa: BLE001
            logger.warning("Semantic Scholar recommendations failed (%s); falling back to OpenAlex.", error)
            fallback_note = "Semantic Scholar was unavailable for this id; fell back to OpenAlex related works."
    _author_email(email)
    work, _ = _resolve_work(paper_id)
    if work is None:
        raise ValueError("Similar-paper lookup needs an OpenAlex-indexed id (DOI, PMID, or OpenAlex id).")
    related_ids = [ref.split("/")[-1] for ref in (work.get("related_works") or [])][:limit]
    records = [_normalize_openalex(item, position)
               for position, item in enumerate(_openalex_works_by_ids(related_ids))]
    logger.warning("Related works are OpenAlex's algorithmic neighbors; verify topical fit before relying on them.")
    if fallback_note:
        logger.warning("%s", fallback_note)
    return {
        "meta": {"operation": "similar", "source": "openalex", "returned": len(records)},
        "paper": {"openalex_id": work["id"].split("/")[-1], "title": work.get("title")},
        "results": records,
    }


@batchable("query")
def facets(query, *, by="year", from_year=None, to_year=None, limit=15, email=None) -> dict:
    """Layer 2 — aggregate a topic into counts grouped by a dimension (a histogram or top-N).

    "by" is one of: year, institution, venue, type, open_access, topic, country. Faceting is an
    OpenAlex capability, so the counts reflect OpenAlex's index only.
    """
    _author_email(email)
    field = FACET_FIELDS.get(by)
    if not field:
        raise ValueError(f"Unknown facet {by!r}; choose from {list(FACET_FIELDS)}.")
    query_builder = Works().search(query) if query else Works()
    if from_year:
        query_builder = query_builder.filter(from_publication_date=f"{from_year}-01-01")
    if to_year:
        query_builder = query_builder.filter(to_publication_date=f"{to_year}-12-31")
    groups: Any = query_builder.group_by(field).get()
    facet_list = [{"key": group.get("key"), "name": group.get("key_display_name"), "count": group.get("count")}
                  for group in groups[:limit]]
    logger.warning("Facet counts are from OpenAlex only, grouped by %s; other databases are not aggregated here.", by)
    return {"meta": {"operation": "facets", "by": by, "count": len(facet_list)}, "results": facet_list}


@batchable("name")
def find_authors(name, *, limit=10, email=None) -> dict:
    """Layer 2 — disambiguate an author name into candidate OpenAlex authors with affiliation and metrics.

    Use this before coauthors/author_works when a name is ambiguous: each candidate carries its
    OpenAlex id, last-known institution, works count, h-index, and citation count.
    """
    _author_email(email)
    matches: Any = Authors().search(name).get(per_page=limit)
    candidates = [_author_summary(author, author["id"]) for author in matches]
    return {"meta": {"operation": "find_authors", "count": len(candidates)}, "query": name,
            "results": candidates}


@batchable("author")
def coauthors(author, *, limit=20, email=None) -> dict:
    """Layer 2 — an author's most frequent collaborators, each enriched with prominence metrics.

    Collaborators are tallied by OpenAlex author id (names are not unique) and ordered by joint-paper
    count; each entry also carries h_index and cited_by_count so they can be re-ranked by prominence.
    "author" is a name or an OpenAlex id.
    """
    _author_email(email)
    anchor = _resolve_openalex_author(author)
    name = anchor["display_name"]
    logger.info("coauthors of %s (%s)", name, anchor["id"])
    works: Any = Works().filter(authorships={"author": {"id": anchor["id"]}}).get(per_page=200)

    joint_counts: Counter = Counter()
    display_names: dict[str, str] = {}
    for work in works:
        for authorship in work["authorships"]:
            collaborator_id = authorship["author"]["id"]
            if collaborator_id == anchor["id"]:
                continue
            joint_counts[collaborator_id] += 1
            display_names[collaborator_id] = authorship["author"]["display_name"]

    top_ids = [collaborator_id for collaborator_id, _ in joint_counts.most_common(limit)]
    metrics = _openalex_authors_by_ids(top_ids)
    result = []
    for collaborator_id in top_ids:
        summary = _author_summary(metrics.get(collaborator_id, {}), collaborator_id)
        summary["name"] = summary["name"] or display_names.get(collaborator_id)
        summary["joint_papers"] = joint_counts[collaborator_id]
        result.append(summary)

    logger.warning(
        "Co-authors were tallied from up to 200 of %s's works (sampled %d); an author with more works "
        "than that may have further collaborators beyond this sample.", name, len(works),
    )
    logger.warning(
        "Collaborators are ordered by joint-paper count; each carries h_index and cited_by_count so "
        "they can also be ranked by prominence."
    )
    return {
        "meta": {"operation": "coauthors", "works_sampled": len(works), "count": len(result)},
        "author": {"name": name, "openalex_id": anchor["id"].split("/")[-1],
                   "works_count": anchor.get("works_count"),
                   "h_index": (anchor.get("summary_stats") or {}).get("h_index")},
        "results": result,
    }


@batchable("author")
def author_works(author, *, coauthor=None, from_year=None, to_year=None, sort=None,
                 limit=20, maximum_results=None, email=None) -> dict:
    """Layer 2 — an author's own works as ranked records, honoring the year window and sort.

    Answers "an author's most cited papers" or "their most recent work". With coauthor set (a name
    or OpenAlex id) it returns only the works the two share — their joint body of work. With
    maximum_results set it deep-pages OpenAlex with a cursor to enumerate beyond the per-page limit,
    for a systematic sweep. "author" and "coauthor" are names or OpenAlex ids.
    """
    _author_email(email)
    anchor = _resolve_openalex_author(author)
    query = Works().filter(authorships={"author": {"id": anchor["id"]}})
    if from_year:
        query = query.filter(from_publication_date=f"{from_year}-01-01")
    if to_year:
        query = query.filter(to_publication_date=f"{to_year}-12-31")
    sort_field = {"citations": "cited_by_count", "date": "publication_date"}.get(sort or "")
    if sort_field:
        query = query.sort(**{sort_field: "desc"})

    raw: Any = []
    if maximum_results:
        for page in query.paginate(per_page=min(200, maximum_results)):
            raw.extend(page)
            if len(raw) >= maximum_results:
                break
        raw = raw[:maximum_results]
    elif coauthor:
        raw = query.get(per_page=200)  # joint subset is smaller, so scan a broad slice of the works
    else:
        raw = query.get(per_page=limit)

    coauthor_summary = None
    if coauthor:
        partner = _resolve_openalex_author(coauthor)
        coauthor_summary = {"name": partner["display_name"], "openalex_id": partner["id"].split("/")[-1]}
        raw = [work for work in raw
               if any(authorship["author"]["id"] == partner["id"] for authorship in work["authorships"])]

    records = [_normalize_openalex(work, position) for position, work in enumerate(raw)]
    ordered = order_results(records, {"sort": sort, "from_year": from_year, "to_year": to_year})
    if from_year or to_year:
        logger.warning(
            "Restricted to publication years %s-%s; works outside this window were excluded.",
            from_year or "any", to_year or "any",
        )
    if coauthor and coauthor_summary:
        logger.warning("Filtered to works shared with %s (the two authors' joint output).", coauthor_summary["name"])
    if not maximum_results:
        logger.warning("Did not enumerate every work; pass maximum_results for a complete, cursor-paged sweep.")
    result = {
        "meta": {"operation": "author_works", "returned": len(ordered)},
        "author": {"name": anchor["display_name"], "openalex_id": anchor["id"].split("/")[-1]},
        "results": ordered,
    }
    if coauthor_summary:
        result["coauthor"] = coauthor_summary
    return result


@batchable("author")
def author_profile(author, *, email=None) -> dict:
    """Layer 2 — fetch the complete OpenAlex author record: full profile with topics,
    concepts, summary_stats, and affiliation history (with years).

    Unlike find_authors/coauthors (which return compact summaries), this returns the
    full OpenAlex author entity, including every topic with its subfield/field hierarchy,
    x_concepts scores, and the full affiliation list annotated with year ranges.

    "author" is an OpenAlex id (A...), an ORCID, or a name (resolved via OpenAlex search).
    """
    _author_email(email)
    raw = _resolve_openalex_author(author)

    affiliations = []
    for affiliation in raw.get("affiliations") or []:
        institution = affiliation.get("institution") or {}
        years = sorted(set(year for year in (affiliation.get("years") or []) if isinstance(year, int)))
        affiliations.append({
            "institution": institution.get("display_name"),
            "ror": institution.get("ror"),
            "years": years,
        })

    result = {
        "display_name": raw.get("display_name"),
        "orcid": raw.get("orcid"),
        "works_count": raw.get("works_count"),
        "cited_by_count": raw.get("cited_by_count"),
        "summary_stats": raw.get("summary_stats"),
        "display_name_alternatives": raw.get("display_name_alternatives"),
        "topics": [
            {"display_name": topic["display_name"],
             "subfield": topic.get("subfield", {}).get("display_name"),
             "field": topic.get("field", {}).get("display_name"),
             "works_count": topic.get("count")}
            for topic in sorted(raw.get("topics") or [], key=lambda entry: entry.get("count", 0), reverse=True)
        ],
        "concepts": [
            {"display_name": concept["display_name"],
             "score": concept.get("score"),
             "level": concept.get("level")}
            for concept in sorted(raw.get("x_concepts") or [], key=lambda entry: entry.get("score", 0), reverse=True)
        ],
        "affiliations": affiliations,
        "last_known_institutions": [
            institution.get("display_name") for institution in (raw.get("last_known_institutions") or [])
        ],
    }
    return {"meta": {"operation": "author_profile"}, "result": result}


