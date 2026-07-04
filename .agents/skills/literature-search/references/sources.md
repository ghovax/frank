# Sources reference

Almost every query goes through the unified engine — the `scholar` package (in `scripts/scholar/`), imported and called from `uv run python` — which fans out to all sources through their official client libraries (pyalex for OpenAlex, arxiv, habanero for Crossref, biopython/Entrez for PubMed, semanticscholar; Europe PMC over its REST API), normalizes, de-duplicates, and ranks for you (see the package docstring for the full request schema). This file holds the verified knowledge the engine does not handle automatically: each source's strengths and quirks, how to run the deeper analytical queries that go beyond topical search, how to retrieve full text and figures, and how to obtain a Semantic Scholar key. Every endpoint and quirk here was verified live.

**Search broad by default.** The whole point is to find the best information easily, so a default request applies no `type`, `open_access`, or year filter, and the fan-out spans preprints (arXiv), reviews, conference papers, and journal articles across every source. Narrow only when the user explicitly asks. Whenever any narrowing filter is applied — open-access only, a publication type, a year window, or simply the per-source result cap — the engine logs it via `logger.warning`; call `scholar.configure_logging()` so those lines stream to stderr, read them from the run output, and relay them to the user, who otherwise cannot see what was excluded. One vocabulary note: the unified `type` uses Crossref names (for example `journal-article`); the engine translates these for OpenAlex, which calls that type `article`.

## Source capability matrix

| Source | Base URL | Auth | Best for | Verified quirks |
|---|---|---|---|---|
| OpenAlex | `api.openalex.org` | none | Analytics: filters, sorting, faceting, citation graph, author/institution entities | Abstracts arrive **inverted**; the client reconstructs them. Grouping by the filtered field collapses (see co-authors below). |
| Crossref | `api.crossref.org` | none | Canonical DOI metadata, publisher records, deposited references | `title`/`container-title` are arrays. |
| arXiv | `export.arxiv.org/api/query` | none | CS/physics/math preprints and free PDFs | **Use `https`** (plain `http` returns empty). Multi-word search defaults to **OR**. |
| PubMed / E-utilities | `eutils.ncbi.nlm.nih.gov/entrez/eutils` | optional `api_key` | Biomedical metadata and the route to PMC full text | Two-step: `esearch` for IDs, then `esummary`/`efetch`. |
| Europe PMC | `www.ebi.ac.uk/europepmc/webservices/rest` | none | Biomedical search returning DOI + PMCID + OA flag, plus citations and full text in one place | Add `resultType=core` for abstract, citation count, and full-text links. |
| Semantic Scholar | `api.semanticscholar.org/graph/v1` | **key recommended** | Relevance, TLDR summaries, SPECTER2 embeddings, citation graph | Keyless access returns HTTP **429** almost immediately. |

## Etiquette: API keys (optional, never hardcoded)

A Semantic Scholar key is read from `S2_API_KEY`. Set it per shell session when you want it:

```bash
export S2_API_KEY="..."                          # Optional; required only for reliable Semantic Scholar access.
```

## Analytical queries beyond topical search (OpenAlex)

The unified client covers topical search with year, open-access, type, and sort options. For structured questions — most-cited, recency windows, co-authorship, faceting, citation graphs — query OpenAlex directly, because it is by far the richest. It exposes seven entity endpoints (`/works`, `/authors`, `/sources`, `/institutions`, `/topics`, `/publishers`, `/funders`) and three levers:

- **`filter=`** (comma means AND): `authorships.author.id:A123`, `authorships.institutions.id:I123`, `publication_year:>2020`, `from_publication_date:2020-01-01`, `cited_by_count:>100`, `type:article`, `is_oa:true`, `primary_topic.id:T123`, `doi:...`, `has_fulltext:true`.
- **`sort=`** (`:asc`/`:desc`): `cited_by_count:desc`, `publication_date:desc`, and `relevance_score:desc` (only alongside `search=`).
- **`group_by=`** (faceting): returns `.group_by[]` as `{key, key_display_name, count}`, for example `group_by=publication_year` for an output histogram or `group_by=open_access.oa_status` for an OA breakdown.

Useful sub-objects on a work: `authorships[].author`, `open_access`, `best_oa_location`, `ids` (doi, pmid, pmcid — good for cross-source joins), `referenced_works[]` (outgoing citations) and `cited_by_api_url` (incoming), and `counts_by_year` (citation trend). An author object carries `summary_stats.h_index` and `works_api_url`.

**Co-authors of an author** must be aggregated client-side, because grouping by `authorships.author.id` while filtering on it collapses to the author alone. Fetch the works and tally collaborators:

```python
from collections import Counter


def top_coauthors(works: list[dict], exclude_author: str, limit: int = 10) -> list[tuple[str, int]]:
    """Count how often each collaborator appears across an author's works, excluding the author themselves."""
    collaborators = Counter(
        authorship["author"]["display_name"]
        for work in works
        for authorship in work["authorships"]
        if authorship["author"]["display_name"] != exclude_author
    )
    return collaborators.most_common(limit)
```

This was verified against Yoshua Bengio (`A5086198262`), returning Aaron Courville (28 joint papers), Pascal Vincent (21), and Kyunghyun Cho (13).

## Full-text routing ladder (the `fulltext` tool)

`scholar.fulltext(paper_id)` does all of this for you: it walks the ladder below and returns every location found under `routes`. With `download=True` it saves the best PDF (to `out_path`, or `out_dir`, or a temp directory) and returns the absolute `pdf_path` plus citation metadata in `paper` — so the driver can attach it to Zotero and have Claude Code read the PDF directly. No text is extracted; reading is done by viewing the PDF. The ladder, in order, is the verified knowledge behind it:

1. **arXiv id** → `https://arxiv.org/pdf/<id>` (verified real PDF).
2. **PMCID** → `https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML`. The PMCID keeps its `PMC` prefix and takes no extra `PMC/` path segment (that returns 404). The NCBI alternative is `efetch.fcgi?db=pmc&id=<numeric>&rettype=xml`.
3. **OpenAlex open-access copy** → `best_oa_location.pdf_url` from the work record.
4. **Crossref DOI** → Unpaywall at `https://api.unpaywall.org/v2/<DOI>?email=<address>`, read `best_oa_location.url_for_pdf`. Unpaywall knows only Crossref-registered DOIs, so arXiv `10.48550/arXiv.*` DOIs 404 — use route 1 for those. The email is required but any address works.
5. **bioRxiv / medRxiv DOI** (`10.1101/...`) → `https://api.biorxiv.org/details/<server>/<doi>` returns a `jatsxml` full-text URL.
6. **CORE** → `https://api.core.ac.uk/v3/search/works/?q=doi:"<doi>"` (trailing slash required) returns `downloadUrl`. A `CORE_API_KEY` env var lifts the rate limit.
7. **Sci-Hub** → a fetch by DOI when no open-access copy exists. The fetcher tries the mirror list in `SCIHUB_MIRRORS` (23 mirrors, ordered by reachability — `sci-hub.st`/`.ru`/`.box`/`.red`/… first, the DNS-flaky `sci-hub.se`/`.te` last), parses the PDF link from the article page's `citation_pdf_url` meta tag, and downloads it directly (disabling certificate verification, since the mirrors require it, and handling DDoS-Guard). Pass `scihub_proxy` (an httpx proxy URL like `socks5://127.0.0.1:7890`) to route it through a proxy. `fulltext` reports `source: "scihub"`. **Sci-Hub froze new uploads around 2021**, so post-2021 papers usually are not here — that is what the Anna's Archive rungs cover.
8. **Anna's Archive — member API** → for papers newer than the Sci-Hub freeze. Resolves the DOI to its canonical SciDB md5 via `/scidb/<doi>` (the record page carries exactly one md5; a redirect to `/search?…` means "no record" → skip), then calls `/dyn/api/fast_download.json?md5=&key=` for a `download_url`. Uses the challenge-free official mirrors `annas-archive.gl`/`.pk`/`.gd` (the `.li`/`.org`/`.se` frontends are JS-walled). **Dormant unless `ANNAS_SECRET_KEY` is set**, and that key needs an **active paid membership** to authorize. `source: "annas_archive"`.
9. **Anna's Archive — keyless slow tier** → the no-membership path (`/slow_download/<md5>/0/<n>`). It sits behind a DDoS-Guard browser challenge that a plain HTTP client cannot pass, so it delivers only when that wall is down or when `FLARESOLVERR_URL` (a running FlareSolverr instance) is set to solve the challenge. **Expect it to be slow — on the order of minutes per paper** (verified end-to-end at ~4 min for one PDF): Anna's deliberately throttles the keyless partner servers, and each challenge solve drives a real browser (~10-15s). Treat it as a reliable last-resort backstop, not a fast path — rung 8 (the member API) is the quick route when a key is available. `source: "annas_archive_slow"`.
10. **Fallback** → when nothing resolves, reason over the abstract and say full text was unavailable. Never fabricate contents you could not retrieve.

## Enabling the Anna's Archive rungs (env vars + FlareSolverr)

Rungs 8 and 9 are off until configured. Both are read from the environment or the project `.env`.

- **`ANNAS_SECRET_KEY`** (rung 8, fast member API) — the secret key from your Anna's Archive Account page. It only authorizes with an **active paid membership**; with `Membership: None` the API returns an error and the rung yields nothing. The key is domain-independent, so it keeps working as the official domains rotate.
- **`FLARESOLVERR_URL`** (rung 9, keyless slow tier) — the base URL of a running FlareSolverr instance (e.g. `http://localhost:8191`). FlareSolverr is a small proxy that drives a headless browser to solve the DDoS-Guard challenge guarding the slow-download endpoints; without it that rung can only succeed on a network where the wall happens to be down.

Check ALWAYS first if it's running, and if not, bring FlareSolverr up with Docker, then point `.env` at it:

```bash
docker run -d --name flaresolverr --restart unless-stopped -p 8191:8191 \
  -e LOG_LEVEL=info ghcr.io/flaresolverr/flaresolverr:latest
# health check (expect {"status": "ok", ...}):
curl -s -X POST http://localhost:8191/v1 -H 'Content-Type: application/json' -d '{"cmd":"sessions.list"}'
```

Then set `FLARESOLVERR_URL=http://localhost:8191` in `.env`. The container takes ~15-30s to become ready on first start (it launches Chrome); each solved request also runs a real browser and Anna's throttles the keyless partner servers, so a slow-tier fetch typically takes **minutes** per paper (~4 min observed for one PDF), not seconds. The code posts `{"cmd":"request.get","url":...}` to `<FLARESOLVERR_URL>/v1` only when a page is actually challenged — unchallenged pages skip the solver entirely. Manage it with `docker {stop,start,logs} flaresolverr`.

## Figures and images from PDFs (the `figures` tool)

`scholar.figures(...)` extracts a PDF's embedded raster figures as JPG images (via PyMuPDF). Give it a `paper_id` (resolved to a PDF through the ladder above), a direct `pdf_url`, or a local `pdf_path`, and an optional `out_dir`. It returns the absolute `workdir` and the list of `images`; read the images. Because this pulls only embedded raster images, vector or composed figures can be missed — to read the whole document (figures in context, text, equations), use `fulltext(download=True)` and have Claude Code read the PDF file directly.

## Zotero (the reference manager)

Every Zotero operation runs through the `zotero_*` tools — the durable, curated side of the loop. They talk to the Zotero **Web API** (`api.zotero.org`) using `ZOTERO_API_KEY` / `ZOTERO_LIBRARY_ID` / `ZOTERO_LIBRARY_TYPE` from the environment or the project `.env`. After `fulltext(download=True)`, `zotero_save(...)` adds the paper by DOI (CrossRef supplies clean metadata) and attaches the returned `pdf_path` in one call; `zotero_items` / `zotero_get` / `zotero_collections` read the library, and `zotero_create` / `zotero_update` / `zotero_delete` / `zotero_attach` do batched CRUD. A DOI from any Zotero item feeds straight back into `citations` / `coauthors` / `similar`. One limit to note: these Web-API tools do metadata quicksearch only — there is no body-text/semantic search over the library; for one paper, `fulltext(download=True)` and read the PDF directly.

**Why the Web API and not the local one (the counter-intuitive part).** Zotero's desktop client also exposes a local API at `http://localhost:23119/api/` that is offline, unauthenticated, free of rate limits, and much faster — which sounds like the obvious choice for code running on the user's own machine. But per the [official docs](https://www.zotero.org/support/dev/web_api/v3/local_api) it is currently **read-only: only GET is accepted, and write requests are unsupported until a future Zotero release**. (It also serves only the locally logged-in user — pass `0` or the numeric user ID — and must be enabled under Settings → Advanced → "Allow other applications on this computer to communicate with Zotero", returning 403 otherwise.) Because `zotero_save` / `zotero_create` / `zotero_update` / `zotero_delete` / `zotero_attach` cannot go through a GET-only endpoint, the engine deliberately uses the Web API **uniformly — for reads as well as writes** — rather than splitting transports and special-casing every call. So even read-only calls default to "online" and hit `api.zotero.org`. This is the weird bit worth remembering: the faster local endpoint exists and is reachable, but it is intentionally unused until it can also write, so that one consistent path handles everything. When Zotero ships local write support, reads (then writes) could be moved there for speed.

## Obtaining a Semantic Scholar API key

Keyless access is throttled to HTTP 429 almost immediately, so a key is needed for reliable use. Request a free one from the form at `https://www.semanticscholar.org/product/api#api-key-form` (it asks for your name, email, and intended use, and the key is emailed to you). Once you have it, export it as `S2_API_KEY`; the client sends it as the `x-api-key` header automatically. The key also lifts limits on the bulk search (`/paper/search/bulk`, up to 1000 results), the citation and reference endpoints, and the SPECTER2 `embedding` field used for semantic re-ranking.
