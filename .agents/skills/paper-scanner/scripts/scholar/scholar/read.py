"""Layer 3 reading: the open-access full-text ladder (arXiv/PMC/Unpaywall/CORE,
then Sci-Hub, then Anna's Archive) and PDF figure extraction."""
import functools
import os
import pathlib
import re
import tempfile
import urllib.parse
from typing import Any

import fitz  # PyMuPDF is published under its historical import name, "fitz".
import httpx
import pyalex

from .analyze import _author_email, _resolve_work
from .common import (
    REQUEST_TIMEOUT,
    _batch_summary,
    _http_get,
    _load_dotenv,
    _parallel_map,
    batchable,
    logger,
)


def _resolve_fulltext_routes(record: dict, email: str) -> list[dict]:
    """Collect every legal open-access full-text location for a record, walking the source ladder."""
    identifiers = record["ids"]
    routes: list[dict] = []
    if identifiers.get("arxiv"):
        routes.append({"route": "arxiv", "pdf_url": f"https://arxiv.org/pdf/{identifiers['arxiv']}"})
    pmcid = identifiers.get("pmcid")
    if not pmcid and identifiers.get("doi"):  # OpenAlex sometimes lacks the PMCID; ask Europe PMC by DOI.
        try:
            search = _http_get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                               {"query": f"DOI:{identifiers['doi']}", "format": "json", "pageSize": 1}).json()
            hit = (search.get("resultList", {}).get("result") or [None])[0]
            if hit and hit.get("pmcid"):
                pmcid = hit["pmcid"]
        except Exception as error:  # noqa: BLE001
            logger.warning("Europe PMC PMCID lookup failed: %s", error)
    if pmcid:
        pmcid = pmcid if str(pmcid).upper().startswith("PMC") else f"PMC{pmcid}"
        routes.append({"route": "europepmc",
                       "xml_url": f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"})
    if (record.get("oa") or {}).get("pdf_url"):
        routes.append({"route": "openalex_oa", "pdf_url": record["oa"]["pdf_url"]})
    doi = identifiers.get("doi")
    if doi:
        try:
            unpaywall: Any = _http_get(f"https://api.unpaywall.org/v2/{doi}", {"email": email}).json()
            best = unpaywall.get("best_oa_location") or {}
            if best.get("url_for_pdf"):
                routes.append({"route": "unpaywall", "pdf_url": best["url_for_pdf"]})
        except Exception as error:  # noqa: BLE001
            logger.warning("Unpaywall lookup failed: %s", error)
    if doi and doi.startswith("10.1101"):  # bioRxiv / medRxiv preprint DOIs
        for server in ("biorxiv", "medrxiv"):
            try:
                detail: Any = _http_get(f"https://api.biorxiv.org/details/{server}/{doi}").json()
                collection = detail.get("collection") or []
                if collection and collection[-1].get("jatsxml"):
                    routes.append({"route": server, "xml_url": collection[-1]["jatsxml"]})
                    break
            except Exception as error:  # noqa: BLE001
                logger.warning("%s lookup failed: %s", server, error)
    if doi:
        try:
            headers = {"Authorization": f"Bearer {os.environ['CORE_API_KEY']}"} if os.environ.get("CORE_API_KEY") else {}
            with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers) as client:
                core: Any = client.get("https://api.core.ac.uk/v3/search/works/",
                                       params={"q": f'doi:"{doi}"', "limit": 1}).json()
            hits = core.get("results") or []
            if hits and hits[0].get("downloadUrl"):
                routes.append({"route": "core", "pdf_url": hits[0]["downloadUrl"]})
        except Exception as error:  # noqa: BLE001
            logger.warning("CORE lookup failed: %s", error)
    return routes


def _download(url: str) -> bytes:
    """Download a URL's raw bytes (used for PDFs)."""
    response = _http_get(url)
    response.raise_for_status()
    return response.content


# Sci-Hub mirror fallbacks, ordered by reachability (probed 2026-06-08).
# Mirrors come and go and DNS resolution varies by network, so the resolver tries them in
# order and stops at the first that yields a %PDF. Tier 1: reachable and served the article
# page in testing. Tier 2: homepage reachable but the article probe failed (403/404/timeout)
# — kept because behavior varies by DOI/headers/network. Tier 3: unreachable at probe time
# (DNS failure) but historically the canonical mirrors — last-resort, may resolve elsewhere.


SCIHUB_MIRRORS = [
    # Tier 1 — reachable + served the DOI page
    "https://sci-hub.st",
    "https://sci-hub.ru",
    "https://sci-hub.box",
    "https://sci-hub.red",
    "https://sci-hub.shop",
    "https://sci-hub.al",
    "https://sci-hub.ren",
    "https://sci-hub.mksa.top",
    "https://www.wellesu.com",
    # Tier 2 — homepage reachable, DOI probe failed at test time
    "https://sci-hub.ee",
    "https://sci-hub.wf",
    "https://sci-hub.org",
    "https://sci-hub.now.sh",
    "https://sci-hub.41610.org",
    "https://sci.bban.top",
    "https://sci-hub.usualwant.com",
    "https://sci-hub.hkvisa.net",
    "https://sci-hub.scihubtw.tw",
    "https://sci-hub.cat",
    "https://sci-hub.do",
    "https://sci-hub.unblockit.id",
    # Tier 3 — canonical mirrors, DNS unreachable at probe time (kept as final fallback)
    "https://sci-hub.se",
    "https://sci-hub.te",
]
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _scihub_pdf_url(html: str) -> str | None:
    """Find the PDF link on a Sci-Hub article page (the citation_pdf_url meta, or an embed/iframe/object)."""
    patterns = [
        r'name="citation_pdf_url"\s+content="([^"]+)"',
        r'<embed[^>]+src="([^"]+\.pdf[^"]*)"',
        r'<iframe[^>]+src="([^"]+\.pdf[^"]*)"',
        r'<object[^>]+data="([^"]+\.pdf[^"]*)"',
        r"location\.href='([^']+\.pdf[^']*)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            return match.group(1)
    return None


def _scihub_pdf(doi: str, scihub_proxy: str | None) -> bytes:
    """Fetch a paper PDF by DOI from a Sci-Hub mirror (the last rung of the full-text ladder).

    Sci-Hub sits behind DDoS-Guard and serves the PDF link in the page (a citation_pdf_url meta tag or
    an embed element), on mirrors that sometimes present invalid certificates. This parses the article
    page and downloads the PDF directly, trying each mirror in turn. Certificate verification is disabled
    because the mirrors require it, and scihub_proxy (an httpx proxy URL such as "socks5://127.0.0.1:7890")
    is used when provided.
    """
    client_arguments: dict[str, Any] = {"headers": {"User-Agent": _BROWSER_UA}, "follow_redirects": True,
                                        "timeout": REQUEST_TIMEOUT, "verify": False}
    if scihub_proxy:
        client_arguments["proxy"] = scihub_proxy
    last_error: Any = None
    for mirror in SCIHUB_MIRRORS:
        try:
            with httpx.Client(**client_arguments) as client:
                pdf_url = _scihub_pdf_url(client.get(f"{mirror}/{doi}").text)
                if not pdf_url:
                    continue
                pdf_url = pdf_url.split("#")[0]
                if pdf_url.startswith("//"):
                    pdf_url = "https:" + pdf_url
                elif pdf_url.startswith("/"):
                    pdf_url = mirror + pdf_url
                response = client.get(pdf_url)
                if response.status_code == 200 and response.content[:4] == b"%PDF":
                    return response.content
        except Exception as error:  # noqa: BLE001
            last_error = error
            logger.warning("Sci-Hub mirror %s failed: %s", mirror, error)
    raise RuntimeError(f"Sci-Hub returned no PDF for {doi} ({last_error})")


# Anna's Archive official member API fallback. Covers papers newer than Sci-Hub's 2021 freeze.
# DORMANT unless ANNAS_SECRET_KEY (an active *paid* membership secret key) is present in the
# environment or a project .env — without it this returns None and changes nothing. Only the
# members-only fast-download JSON API is automatable: the free "slow" tier and the .li/.org/.se
# web frontends sit behind a JS anti-bot wall (DDoS-Guard) that a plain HTTP client cannot pass.
# These three official mirror domains served the API and the md5 pages challenge-free when probed
# (2026-06-08); they rotate, so cross-check against Anna's Wikipedia page if all three fail.


ANNAS_DOMAINS = ["https://annas-archive.gl", "https://annas-archive.pk", "https://annas-archive.gd"]
ANNAS_SLOW_SERVERS = 5  # partner-server slots tried per md5 on the keyless slow-download tier


def _annas_resolve_md5(doi: str, client: httpx.Client) -> str | None:
    """Resolve a DOI to its Anna's Archive md5 via the canonical SciDB record page.

    `/scidb/<doi>` redirects to the single canonical record (one md5, resolved by Anna itself) when
    the paper is in SciDB, or to the fuzzy `/search?...` page when it is not. Only the record page is
    trusted — a redirect to search means "no SciDB record" and returns None rather than guessing
    from a similarly-named paper. Pages are fetched through _solve_challenge, so resolution survives a
    JS wall on the record page when FLARESOLVERR_URL is configured.
    """
    for domain in ANNAS_DOMAINS:
        html, _ = _solve_challenge(f"{domain}/scidb/{urllib.parse.quote(doi)}", client)
        if not html or re.search(r"<title>[^<]*- Search - Anna", html):
            continue  # no canonical record (fuzzy search fallback), or still challenged
        match = re.search(r"/md5/([0-9a-f]{32})", html)
        if match:
            return match.group(1)
    return None


def _annas_client_args(scihub_proxy: str | None) -> dict:
    """Shared httpx.Client kwargs for Anna's Archive (browser UA, redirects, optional proxy)."""
    args: dict[str, Any] = {"headers": {"User-Agent": _BROWSER_UA}, "follow_redirects": True,
                            "timeout": REQUEST_TIMEOUT, "verify": False}
    if scihub_proxy:
        args["proxy"] = scihub_proxy
    return args


def _annas_fast_download_md5(md5: str, key: str, client: httpx.Client) -> bytes | None:
    """Download a file by md5 via Anna's members-only fast-download JSON API (needs a paid key)."""
    for domain in ANNAS_DOMAINS:
        try:
            response = client.get(f"{domain}/dyn/api/fast_download.json", params={"md5": md5, "key": key})
            payload = response.json()
        except Exception as error:  # noqa: BLE001
            logger.warning("Anna's Archive API on %s failed: %s", domain, error)
            continue
        download_url = payload.get("download_url")
        if not download_url:
            logger.warning("Anna's Archive API error for md5 %s: %s", md5, payload.get("error"))
            if response.status_code in (401, 403):
                break  # an auth/membership error is the same on every mirror; stop early
            continue
        if (data := client.get(download_url).content)[:4] == b"%PDF":
            return data
        logger.warning("Anna's Archive download_url did not return a PDF for md5 %s", md5)
    return None


def _annas_pdf(doi: str, scihub_proxy: str | None) -> bytes | None:
    """Fetch a paper PDF by DOI from Anna's Archive's member API, or None if unavailable.

    Returns None (silently dormant) when no ANNAS_SECRET_KEY is configured. With a key: resolve the
    DOI to an md5, then download via the members-only fast-download API.
    """
    _load_dotenv()
    key = os.environ.get("ANNAS_SECRET_KEY")
    if not key:
        return None
    with httpx.Client(**_annas_client_args(scihub_proxy)) as client:
        md5 = _annas_resolve_md5(doi, client)
        if not md5:
            logger.warning("Anna's Archive: no exact md5 match for %s", doi)
            return None
        return _annas_fast_download_md5(md5, key, client)


def _solve_challenge(url: str, client: httpx.Client) -> tuple[str | None, dict[str, str]]:
    """Fetch a URL, transparently passing any DDoS-Guard / Cloudflare browser challenge.

    Returns (html, cookies). A plain GET is tried first; if it is blocked by a JS anti-bot wall and
    a FLARESOLVERR_URL is configured (a running FlareSolverr instance, which solves DDoS-Guard), the
    request is routed through it and the solved HTML + cookies are returned. Without a solver a
    challenged page yields (None, {}) — the caller treats that as "unavailable" rather than failing.
    """
    try:
        response = client.get(url)
        if "DDoS-Guard" not in response.text and "Checking your browser" not in response.text:
            return response.text, {}
    except Exception as error:  # noqa: BLE001
        logger.warning("GET %s failed: %s", url, error)
    _load_dotenv()
    solver = os.environ.get("FLARESOLVERR_URL")
    if not solver:
        logger.warning("%s is behind a browser challenge; set FLARESOLVERR_URL to bypass it.", url)
        return None, {}
    try:
        # FlareSolverr drives a real browser, so the POST must outlast its own solve budget
        # (maxTimeout) — the default 30s client timeout would abort mid-solve.
        solve_ms = 60000
        solved = client.post(f"{solver.rstrip('/')}/v1",
                             json={"cmd": "request.get", "url": url, "maxTimeout": solve_ms},
                             timeout=httpx.Timeout(solve_ms / 1000 + 15)).json()
        solution = solved.get("solution") or {}
        cookies = {cookie["name"]: cookie["value"] for cookie in solution.get("cookies", []) if "name" in cookie}
        return solution.get("response"), cookies
    except Exception as error:  # noqa: BLE001
        logger.warning("FlareSolverr could not solve %s: %s", url, error)
        return None, {}


def _annas_slow_download_md5(md5: str, client: httpx.Client) -> bytes | None:
    """Download a file by md5 via Anna's keyless "slow download" tier (behind DDoS-Guard).

    Needs no membership key, but the slow endpoints sit behind a browser challenge, so a PDF comes
    back only on a network/domain where the wall is down or when FLARESOLVERR_URL is set (see
    _solve_challenge). Each md5 is offered by several partner servers (/slow_download/<md5>/0/N); the
    waitlist page is parsed for the final external file link, which is then downloaded.
    """
    for domain in ANNAS_DOMAINS:
        for server_index in range(ANNAS_SLOW_SERVERS):
            page_url = f"{domain}/slow_download/{md5}/0/{server_index}"
            html, cookies = _solve_challenge(page_url, client)
            if not html:
                continue
            # The page links the file on an external partner server (an absolute http(s) URL
            # ending in .pdf, off the annas-archive domain) — match that, not the internal
            # /md5/ record links (which also contain the md5) or the donation anchors.
            patterns = (r'href="(https?://(?!annas-archive)[^"]+\.pdf[^"]*)"',
                        r'href="(https?://(?!annas-archive)[^"]+/d/[^"]+)"')
            link = next((match.group(1) for pattern in patterns
                         for match in [re.search(pattern, html, re.I)] if match), None)
            if not link:
                continue
            if link.startswith("/"):
                link = domain + link
            try:
                data = client.get(link, cookies=cookies).content
            except Exception as error:  # noqa: BLE001
                logger.warning("Anna's Archive (slow) link fetch failed for md5 %s: %s", md5, error)
                continue
            if data[:4] == b"%PDF":
                return data
    return None


def _annas_slow_pdf(doi: str, scihub_proxy: str | None) -> bytes | None:
    """Fetch a paper PDF by DOI via Anna's keyless slow tier (needs FlareSolverr; see _solve_challenge)."""
    with httpx.Client(**_annas_client_args(scihub_proxy)) as client:
        md5 = _annas_resolve_md5(doi, client)
        if not md5:
            logger.warning("Anna's Archive (slow): no exact md5 match for %s", doi)
            return None
        return _annas_slow_download_md5(md5, client)


def _annas_md5_from_isbn(isbn: str, client: httpx.Client) -> str | None:
    """Resolve an ISBN to an Anna's Archive md5 via its search page (top result).

    Books have no DOI/SciDB record, so resolution is by ISBN search rather than the canonical /scidb
    path used for papers; the first /md5/ link on the results page is the top-ranked match. Fetched
    through _solve_challenge so it survives the DDoS-Guard wall when FLARESOLVERR_URL is set.
    """
    clean = re.sub(r"[^0-9Xx]", "", isbn)
    for domain in ANNAS_DOMAINS:
        html, _ = _solve_challenge(f"{domain}/search?q={urllib.parse.quote(clean)}", client)
        if not html:
            continue
        match = re.search(r"/md5/([0-9a-f]{32})", html)
        if match:
            return match.group(1)
    logger.warning("Anna's Archive: no md5 match for ISBN %s", isbn)
    return None


def _acquire_book(isbn: str, scihub_proxy: str | None = None) -> tuple[bytes | None, str | None]:
    """Return (pdf_bytes, source) for a book by ISBN from Anna's Archive: the members-only fast tier
    if ANNAS_SECRET_KEY is set, then the keyless slow tier. Books are a shadow-library strength,
    unlike post-2021 journal papers, so this is the realistic way to acquire a textbook PDF."""
    _load_dotenv()
    with httpx.Client(**_annas_client_args(scihub_proxy)) as client:
        md5 = _annas_md5_from_isbn(isbn, client)
        if not md5:
            return None, None
        key = os.environ.get("ANNAS_SECRET_KEY")
        if key and (data := _annas_fast_download_md5(md5, key, client)):
            return data, "annas_archive"
        if data := _annas_slow_download_md5(md5, client):
            return data, "annas_archive_slow"
    return None, None


def _acquire_pdf(record: dict, routes: list[dict], scihub_proxy: str | None = None) -> tuple[bytes | None, str | None]:
    """Return (pdf_bytes, source) walking the ladder: open-access, Sci-Hub, then Anna's Archive
    (member API if a key is set, then the keyless slow-download tier)."""
    pdf_url = next((route.get("pdf_url") for route in routes if route.get("pdf_url")), None)
    if pdf_url:
        try:
            return _download(pdf_url), "open_access"
        except Exception as error:  # noqa: BLE001
            logger.warning("Open-access PDF download failed: %s", error)
    doi = record["ids"].get("doi")
    if doi:
        try:
            return _scihub_pdf(doi, scihub_proxy), "scihub"
        except Exception as error:  # noqa: BLE001
            logger.warning("Sci-Hub fetch failed: %s", error)
        for fetch, source in ((_annas_pdf, "annas_archive"), (_annas_slow_pdf, "annas_archive_slow")):
            try:
                annas_bytes = fetch(doi, scihub_proxy)
                if annas_bytes:
                    return annas_bytes, source
            except Exception as error:  # noqa: BLE001
                logger.warning("%s fetch failed: %s", source, error)
    return None, None


def _render_pdf_playwright(url: str) -> bytes | None:
    """Render a URL to a self-contained PDF with headless Chromium (Playwright).

    Faithful for any site, including JS-rendered pages — the page is loaded in a real browser and
    printed to PDF with backgrounds. Returns None (and logs) if Playwright or its browser is missing
    or the render fails, so the caller can fall back. Requires `playwright` and a Chromium install
    (`uv run playwright install chromium`).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright not installed; cannot render %s to PDF (run: uv run playwright install chromium)", url)
        return None
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="load", timeout=60000)
                page.wait_for_timeout(1500)  # let late/lazy content settle before printing
                data = page.pdf(format="A4", print_background=True,
                                margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"})
            finally:
                browser.close()
        return data if data[:4] == b"%PDF" else None
    except Exception as error:  # noqa: BLE001
        logger.warning("Playwright render failed for %s: %s", url, error)
        return None


def _acquire_webpage(url: str) -> tuple[bytes | None, str, str | None]:
    """Return (bytes, extension, source) for a webpage artifact.

    Always render the page to a self-contained PDF with headless Chromium (Playwright) — faithful for
    any site, including JS-heavy ones, with no per-site special cases. Falls back to a raw single-fetch
    HTML snapshot only if the browser is unavailable or the render fails.
    """
    if (data := _render_pdf_playwright(url)) is not None:
        return data, ".pdf", "playwright_pdf"
    try:
        return _download(url), ".html", "html_snapshot"
    except Exception as error:  # noqa: BLE001
        logger.warning("Webpage snapshot failed for %s: %s", url, error)
        return None, ".html", None


def book_fulltext(isbn: str, *, download: bool = False, out_path: str | None = None) -> dict:
    """Acquire a book PDF by ISBN from Anna's Archive (the books shadow library).

    Resolves the ISBN to an md5 and downloads via the members-only fast tier (if ANNAS_SECRET_KEY is
    set) then the keyless slow tier (which needs a running FlareSolverr at FLARESOLVERR_URL — see
    _solve_challenge). With download=True the PDF is saved (to out_path or a temp file) and its path
    returned, ready to attach to a Zotero book item via zotero_attach. Returns
    {"meta", "isbn", "found", "source", "pdf_path"} — pdf_path is None when no PDF could be obtained.
    """
    data, source = _acquire_book(isbn)
    pdf_path = None
    if data and download:
        target = (pathlib.Path(out_path) if out_path
                  else pathlib.Path(tempfile.gettempdir()) / f"book_{re.sub(r'[^0-9Xx]', '', isbn)}.pdf")
        target.write_bytes(data)
        pdf_path = str(target.resolve())
    return {"meta": {"operation": "book_fulltext"}, "isbn": isbn,
            "found": bool(data), "source": source, "pdf_path": pdf_path}


def webpage_snapshot(url: str, *, out_path: str | None = None) -> dict:
    """Save an artifact for a webpage: a full-page PDF rendered with headless Chromium (Playwright),
    falling back to a raw HTML snapshot only if the browser is unavailable.

    The saved file is suitable for attaching to a Zotero webpage item via zotero_attach. Returns
    {"meta", "url", "found", "source", "path", "content_type"} — path is None when nothing was saved.
    """
    data, ext, source = _acquire_webpage(url)
    path = None
    if data:
        if out_path:
            target = pathlib.Path(out_path)
        else:
            stem = re.sub(r"\W+", "_", url).strip("_")[-60:]
            target = pathlib.Path(tempfile.gettempdir()) / f"snapshot_{stem}{ext}"
        target.write_bytes(data)
        path = str(target.resolve())
    return {"meta": {"operation": "webpage_snapshot"}, "url": url, "found": bool(data),
            "source": source, "path": path, "content_type": "application/pdf" if ext == ".pdf" else "text/html"}


def _extract_figure_images(pdf_bytes: bytes, out_dir: str | None = None) -> tuple[str, list[str]]:
    """Extract the embedded raster figures from a PDF as JPG images, into out_dir or a fresh temp directory.

    JPEG cannot hold alpha or CMYK, so such pixmaps are converted to RGB first.
    """
    workdir = (pathlib.Path(out_dir) if out_dir else pathlib.Path(tempfile.mkdtemp(prefix="paperscanner_"))).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    image_paths: list[str] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as opened:
        document: Any = opened
        for page_number, page in enumerate(document, start=1):
            for index, image_ref in enumerate(page.get_images(full=True)):
                try:
                    pixmap = fitz.Pixmap(document, image_ref[0])
                    if pixmap.colorspace is None:
                        continue  # An image mask / stencil, not a real figure.
                    if pixmap.alpha or pixmap.n >= 4:
                        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                    out = workdir / f"page-{page_number:03d}-figure-{index:02d}.jpg"
                    pixmap.save(out, jpg_quality=85)
                    image_paths.append(str(out))
                except Exception as error:  # noqa: BLE001
                    logger.warning("Skipped an embedded image (page %s, #%s): %s", page_number, index, error)
    return str(workdir), image_paths


def _resolve_pdf_bytes(paper_id, pdf_url, pdf_path, email, scihub_proxy) -> tuple[bytes, str]:
    """Obtain (pdf_bytes, source) for the figures function from a local path, a URL, or a paper id."""
    if pdf_path:
        return pathlib.Path(pdf_path).read_bytes(), "local"
    if pdf_url:
        return _download(pdf_url), "open_access"
    pyalex.config.email = email or None
    _, record = _resolve_work(paper_id)
    resolved_email = email
    pdf_bytes, source = _acquire_pdf(record, _resolve_fulltext_routes(record, resolved_email), scihub_proxy)
    if not pdf_bytes or source is None:
        raise ValueError("No PDF could be obtained for this id (no open-access copy, and Sci-Hub returned nothing).")
    return pdf_bytes, source


@batchable("paper_id")
def fulltext(paper_id, *, download=False, out_path=None, out_dir=None, email=None, scihub_proxy=None) -> dict:
    """Layer 3 — resolve open-access full-text locations for a paper, and optionally download the PDF.

    Walks the open-access ladder (arXiv, Europe PMC / PMC, the OpenAlex open-access copy, Unpaywall,
    bioRxiv/medRxiv, and CORE) and returns every location under "routes"; when none exist the PDF is
    fetched from Sci-Hub as a last resort. With download=True it saves the best PDF and returns its
    "pdf_path" (and the "source" it came from), so the caller can attach it to Zotero and have Claude
    Code read the PDF directly. Choose where it goes: pass out_path for an exact file path, or out_dir
    for a directory (the filename is derived from the id); with neither, a temp directory is used. No
    text is extracted — reading is done by viewing the PDF. The returned "paper" carries the citation
    metadata (title, authors, year, venue, ids).
    """
    _author_email(email)
    _, record = _resolve_work(paper_id)
    resolved_email = email
    routes = _resolve_fulltext_routes(record, resolved_email)

    pdf_path = None
    source = None
    if download:
        pdf_bytes, source = _acquire_pdf(record, routes, scihub_proxy)
        if pdf_bytes:
            if out_path:
                target = pathlib.Path(out_path)
            else:
                directory = pathlib.Path(out_dir) if out_dir else pathlib.Path(tempfile.mkdtemp(prefix="paperscanner_"))
                stem = (record["ids"].get("doi") or record["ids"].get("arxiv") or "paper").replace("/", "_")
                target = directory / f"{stem}.pdf"
            target = target.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pdf_bytes)
            pdf_path = str(target)

    if download and not pdf_path:
        logger.warning("No PDF could be obtained (no open-access copy, and Sci-Hub returned nothing).")
    elif not routes:
        logger.warning("No open-access full text was located; only the abstract and metadata are available.")
    return {
        "meta": {"operation": "fulltext", "source": source},
        "paper": {"title": record["title"], "authors": record["authors"], "year": record["year"],
                  "venue": record["venue"], "ids": record["ids"], "oa": record.get("oa")},
        "routes": routes,
        "pdf_path": pdf_path,
    }


def _figures_single(*, paper_id=None, pdf_url=None, pdf_path=None, out_dir=None, email=None, scihub_proxy=None) -> dict:
    """Layer 3 — extract the embedded figure images from a paper's PDF as JPG files.

    Provide one source: a paper "paper_id" (resolved to a PDF through the full-text ladder), a direct
    "pdf_url", or a local "pdf_path". Images go to out_dir, or a temp directory when it is omitted; the
    path is returned as "workdir". This pulls the PDF's embedded
    raster images, so vector or composed figures can be missed — to read the whole document (figures in
    context, text, equations), download the PDF with fulltext(..., download=True) and have Claude Code
    read that file directly.
    """
    if not (paper_id or pdf_url or pdf_path):
        raise ValueError("figures requires one of: paper_id, pdf_url, or pdf_path.")
    pdf_bytes, source = _resolve_pdf_bytes(paper_id, pdf_url, pdf_path, email, scihub_proxy)
    workdir, images = _extract_figure_images(pdf_bytes, out_dir)
    return {"meta": {"operation": "figures", "source": source},
            "workdir": workdir, "count": len(images), "images": images}


@functools.wraps(_figures_single)
def figures(*, paper_id=None, pdf_url=None, pdf_path=None, out_dir=None, email=None, scihub_proxy=None):  # noqa: F811
    sources = [("paper_id", paper_id), ("pdf_url", pdf_url), ("pdf_path", pdf_path)]
    listed = next(((name, value) for name, value in sources if isinstance(value, (list, tuple))), None)
    base_kwargs = {"paper_id": paper_id, "pdf_url": pdf_url, "pdf_path": pdf_path,
                   "out_dir": out_dir, "email": email, "scihub_proxy": scihub_proxy}
    if listed is None:
        return _figures_single(**base_kwargs)
    name, values = listed

    def call_one(value):
        try:
            return _figures_single(**{**base_kwargs, name: value})
        except Exception as error:  # noqa: BLE001
            return {"_error": f"{type(error).__name__}: {error}", "input": value}

    return _batch_summary("figures", _parallel_map(call_one, list(values)))
