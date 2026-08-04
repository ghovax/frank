"""Zotero Web API integration: full item CRUD, PDF upload, library read/search,
and the one-call save pipeline."""
import hashlib
import json
import os
import pathlib
import re
import tempfile
import time
import urllib.parse
from typing import Any

import httpx

from .analyze import _author_email, _resolve_work
from .common import (
    REQUEST_TIMEOUT,
    _batch_summary,
    _http_get,
    _load_dotenv,
    _parallel_map,
    logger,
)
from .read import _acquire_pdf, _resolve_fulltext_routes


ZOTERO_API_BASE = "https://api.zotero.org"
ZOTERO_WRITE_CHUNK = 50  # The Web API accepts at most 50 objects per create/update/delete request.


def _zotero_config() -> tuple[str, str]:
    """Return (api_key, library_base_url) from the environment / .env, or raise a clear error."""
    _load_dotenv()
    api_key = os.environ.get("ZOTERO_API_KEY")
    library_id = os.environ.get("ZOTERO_LIBRARY_ID")
    library_type = (os.environ.get("ZOTERO_LIBRARY_TYPE") or "user").lower()
    if not (api_key and library_id):
        raise RuntimeError(
            "Zotero Web API credentials are missing: set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID "
            "(and optionally ZOTERO_LIBRARY_TYPE=user|group) in the environment or a project .env."
        )
    prefix = "groups" if library_type.startswith("group") else "users"
    return api_key, f"{ZOTERO_API_BASE}/{prefix}/{library_id}"


def _zotero_request(method: str, url: str, *, headers: dict | None = None,
                    params: dict | None = None, content: Any = None) -> httpx.Response:
    """Issue an authenticated Zotero Web API request with the shared timeout and version header."""
    api_key, _ = _zotero_config()
    merged = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3", **(headers or {})}
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        response = client.request(method, url, headers=merged, params=params, content=content)
    return response


_ZOTERO_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _zotero_get(url: str, params: dict | None = None, retries: int = 3) -> httpx.Response:
    """GET a Zotero endpoint, retrying transient failures (rate limits, 5xx, transport errors).

    GETs are idempotent, so retrying is safe — unlike writes. Honors a Retry-After header when present.
    """
    last_error: Exception | None = None
    response: httpx.Response | None = None
    for attempt in range(retries):
        try:
            response = _zotero_request("GET", url, params=params)
            if response.status_code in _ZOTERO_TRANSIENT_STATUS and attempt < retries - 1:
                time.sleep(min(float(response.headers.get("Retry-After", attempt + 1)), 5.0))
                continue
            return response
        except (httpx.TransportError, httpx.TimeoutException) as error:
            last_error = error
            time.sleep(attempt + 1)
    if response is not None:
        return response
    raise last_error  # type: ignore[misc]


def _zotero_library_version() -> int:
    """Read the library's current version from the Last-Modified-Version header (for write preconditions)."""
    _, base = _zotero_config()
    response = _zotero_get(f"{base}/items", params={"limit": 1, "format": "json"})
    response.raise_for_status()
    return int(response.headers.get("Last-Modified-Version", 0))


def _zotero_versioned_write(method: str, url: str, *, params: dict | None = None,
                            content: Any = None, extra_headers: dict | None = None) -> httpx.Response:
    """Issue a write guarded by the current library version, retrying on a 412 version conflict.

    Concurrent writes can each read the same library version and race, so the loser comes back 412
    Precondition Failed; re-reading the version and retrying resolves it.
    """
    response: httpx.Response | None = None
    for attempt in range(3):
        headers = {"If-Unmodified-Since-Version": str(_zotero_library_version()), **(extra_headers or {})}
        response = _zotero_request(method, url, headers=headers, params=params, content=content)
        if response.status_code == 412 and attempt < 2:
            logger.warning("Zotero write hit a version conflict (412); re-reading version and retrying.")
            continue
        break
    return response  # type: ignore[return-value]


def _zotero_post_items(objects: list[dict]) -> dict:
    """POST a batch of item objects (create when no key, update — PATCH semantics — when key present).

    Chunks the list into the API's 50-object limit, using the library version as the write precondition.
    Returns {"success": [{"index", "key"}], "unchanged": [...], "failed": [...], "library_version": v}.
    """
    _, base = _zotero_config()
    out: dict[str, Any] = {"success": [], "unchanged": [], "failed": [], "library_version": None}
    for offset in range(0, len(objects), ZOTERO_WRITE_CHUNK):
        chunk = objects[offset:offset + ZOTERO_WRITE_CHUNK]
        response = _zotero_versioned_write(
            "POST", f"{base}/items",
            extra_headers={"Content-Type": "application/json"},
            content=json.dumps(chunk),
        )
        response.raise_for_status()
        payload = response.json()
        out["library_version"] = response.headers.get("Last-Modified-Version") or out["library_version"]
        for index, key in (payload.get("success") or {}).items():
            out["success"].append({"index": offset + int(index), "key": key})
        for index, key in (payload.get("unchanged") or {}).items():
            out["unchanged"].append({"index": offset + int(index), "key": key})
        for index, info in (payload.get("failed") or {}).items():
            out["failed"].append({"index": offset + int(index), **info})
    return out


def zotero_create(items) -> dict:
    """Create Zotero item(s) from editable-JSON dicts (itemType + fields), 50 per request.

    Accepts a single item dict or a list of them. Each dict is a Zotero item template — e.g.
    {"itemType": "journalArticle", "title": ..., "creators": [{"creatorType": "author", "firstName": ...,
    "lastName": ...}], "DOI": ..., "tags": [{"tag": ...}], "collections": ["<key>"]}. Returns the unified
    envelope {"meta": {operation, count, ok, failed, unchanged}, "results": [{index, key}], "unchanged":
    [...], "failed": [...], "library_version": v} — "results" holds the created keys with their input index.
    """
    items = items if isinstance(items, list) else [items]
    outcome = _zotero_post_items(items)
    logger.info("zotero_create: %d created, %d failed", len(outcome["success"]), len(outcome["failed"]))
    return {"meta": {"operation": "zotero_create", "count": len(items), "ok": len(outcome["success"]),
                     "failed": len(outcome["failed"]), "unchanged": len(outcome["unchanged"])},
            "results": outcome["success"], "unchanged": outcome["unchanged"],
            "failed": outcome["failed"], "library_version": outcome["library_version"]}


def zotero_update(updates: list[dict]) -> dict:
    """Update existing item(s) in one or more 50-object requests (POST = PATCH semantics).

    Accepts a single update dict or a list. Each dict MUST carry the item's "key"; include only the fields
    you want changed — omitted fields are left untouched, and an empty string/array clears a field. Every
    field can be set at once. The library version is sent as the write precondition, so individual "version"
    properties are optional. Returns the unified envelope {"meta": {operation, count, ok, failed, unchanged},
    "results": [{index, key}], "unchanged": [...], "failed": [...], "library_version": v}.
    """
    updates = updates if isinstance(updates, list) else [updates]
    missing = [index for index, update in enumerate(updates) if not update.get("key")]
    if missing:
        raise ValueError(f"zotero_update: every object needs a 'key'; missing at indexes {missing}.")
    outcome = _zotero_post_items(updates)
    logger.info("zotero_update: %d updated, %d failed", len(outcome["success"]), len(outcome["failed"]))
    return {"meta": {"operation": "zotero_update", "count": len(updates), "ok": len(outcome["success"]),
                     "failed": len(outcome["failed"]), "unchanged": len(outcome["unchanged"])},
            "results": outcome["success"], "unchanged": outcome["unchanged"],
            "failed": outcome["failed"], "library_version": outcome["library_version"]}


def zotero_delete(keys) -> dict:
    """Delete item(s) by key, up to 50 per request, using the library version as precondition.

    Accepts a single key string or a list of keys. Returns the unified envelope
    {"meta": {operation, count, ok}, "results": [<deleted key>, ...]}.
    """
    keys = keys if isinstance(keys, list) else [keys]
    _, base = _zotero_config()
    deleted = 0
    for offset in range(0, len(keys), ZOTERO_WRITE_CHUNK):
        chunk = keys[offset:offset + ZOTERO_WRITE_CHUNK]
        response = _zotero_versioned_write("DELETE", f"{base}/items", params={"itemKey": ",".join(chunk)})
        response.raise_for_status()
        deleted += len(chunk)
    logger.info("zotero_delete: %d deleted", deleted)
    return {"meta": {"operation": "zotero_delete", "count": len(keys), "ok": deleted}, "results": list(keys)}


def _zotero_all_collections() -> list[dict]:
    """Fetch every collection in the library (paged), as raw Zotero rows {key, version, data, meta}."""
    _, base = _zotero_config()
    collected: list[dict] = []
    page, start = 100, 0
    while True:
        response = _zotero_get(f"{base}/collections", params={"format": "json", "limit": page, "start": start})
        response.raise_for_status()
        rows = response.json()
        collected.extend(rows)
        if len(rows) < page:
            break
        start += page
    return collected


def _zotero_resolve_collection(collection: str, rows: list[dict]) -> str:
    """Resolve a collection key or (case-insensitive) name to a collection key, given the collection rows.

    An exact key match wins; otherwise an exact name match, then a unique substring match. Raises ValueError
    when nothing matches, and warns (using the first) when a name is ambiguous.
    """
    for collection_row in rows:
        if collection_row["key"] == collection:
            return collection_row["key"]
    target_name = collection.lower()
    exact_matches = [row for row in rows if (row.get("data", {}).get("name") or "").lower() == target_name]
    substring_matches = [row for row in rows if target_name in (row.get("data", {}).get("name") or "").lower()]
    matches = exact_matches or substring_matches
    if not matches:
        raise ValueError(
            f"No Zotero collection matches {collection!r}. Call zotero_collections() to list names and keys."
        )
    if len(matches) > 1:
        ambiguous_names = ", ".join(f'{row["data"].get("name")} ({row["key"]})' for row in matches[:6])
        logger.warning("Collection %r is ambiguous; using the first of: %s", collection, ambiguous_names)
    return matches[0]["key"]


def _zotero_collection_descendants(key: str, rows: list[dict]) -> list[str]:
    """Return `key` plus all of its descendant collection keys, given the flat collection rows."""
    children_by_parent: dict[str, list[str]] = {}
    for row in rows:
        parent = row.get("data", {}).get("parentCollection")  # a parent key, or False at the root
        if parent:
            children_by_parent.setdefault(parent, []).append(row["key"])
    descendants, pending = [], [key]
    while pending:
        current = pending.pop()
        descendants.append(current)
        pending.extend(children_by_parent.get(current, []))
    return descendants


def zotero_items(*, query: str | None = None, item_type: str | None = "-attachment",
                 tag: str | None = None, collection: str | None = None, subcollections: bool = False,
                 limit: int | None = None, full: bool = False) -> dict:
    """Read items from the library (paged): the full list, a quicksearch, and/or one collection.

    With no `query` this returns the entire library (paging through every item); with a `query` it runs
    Zotero's quicksearch (matched against title/creators/year, or everything). `item_type` defaults to
    excluding attachments (pass e.g. "journalArticle" to filter, or None for all types); `tag` filters by
    tag. `collection` restricts the read to a single collection, given either its **key** or its **name**
    (case-insensitive, resolved via `zotero_collections`); set `subcollections=True` to also include every
    descendant collection (de-duplicated across the tree). `query`/`item_type`/`tag` still apply on top of a
    collection, so you can quicksearch within it. `limit` caps the total returned. By default each item is
    the compact form {key, version, doi, title, item_type} — good for dedup and inspection; pass `full=True`
    to get the complete editable JSON under "data" (every field) instead. For a few specific items by key
    (and their child notes/attachments), use `zotero_get`. Returns the unified envelope
    {"meta": {operation, count}, "results": [...]}.
    """
    _, base = _zotero_config()
    params: dict[str, Any] = {"format": "json", "include": "data"}
    if item_type:
        params["itemType"] = item_type
    if query:
        params["q"] = query
        params["qmode"] = "everything"
    if tag:
        params["tag"] = tag

    # Without a collection, read the whole library; with one, read that collection's item endpoint(s).
    endpoints = [f"{base}/items"]
    if collection:
        all_collections = _zotero_all_collections()
        root_key = _zotero_resolve_collection(collection, all_collections)
        collection_keys = _zotero_collection_descendants(root_key, all_collections) if subcollections else [root_key]
        endpoints = [f"{base}/collections/{collection_key}/items" for collection_key in collection_keys]

    collected: list[dict] = []
    seen: set[str] = set()
    page = 100
    for endpoint in endpoints:
        start = 0
        while True:
            response = _zotero_get(endpoint, params={**params, "limit": page, "start": start})
            response.raise_for_status()
            rows = response.json()
            for row in rows:
                if row["key"] in seen:  # an item can live in several (sub)collections
                    continue
                seen.add(row["key"])
                data = row.get("data", {})
                if full:
                    collected.append({"key": row["key"], "version": row["version"], "data": data})
                else:
                    collected.append({"key": row["key"], "version": row["version"],
                                      "doi": (data.get("DOI") or "").lower() or None,
                                      "title": data.get("title"), "item_type": data.get("itemType")})
            if len(rows) < page or (limit and len(collected) >= limit):
                break
            start += page
        if limit and len(collected) >= limit:
            break
    collected = collected[:limit] if limit else collected
    return {"meta": {"operation": "zotero_items", "count": len(collected)}, "results": collected}


def zotero_collections(query: str | None = None) -> dict:
    """List the library's collections — to discover a collection's key (and its place in the tree).

    With no `query`, returns every collection; with `query`, returns those whose name contains it
    (case-insensitive). Each record is {key, name, parent, item_count, subcollection_count, path}, where
    `path` is the slash-joined name hierarchy from the root, sorted alphabetically by path. Pass a returned
    `key` (or a name) as the `collection=` argument of `zotero_items` to read just that collection's items.
    Returns the unified envelope {"meta": {operation, count}, "results": [...]}.
    """
    rows = _zotero_all_collections()
    rows_by_key = {row["key"]: row for row in rows}

    def path_of(row: dict) -> str:
        names, visited = [], set()
        current: dict | None = row
        while current is not None and current["key"] not in visited:
            visited.add(current["key"])
            names.append(current.get("data", {}).get("name") or "")
            parent = current.get("data", {}).get("parentCollection")
            current = rows_by_key.get(parent) if parent else None
        return " / ".join(reversed(names))

    query_lower = (query or "").lower()
    collections: list[dict] = []
    for row in rows:
        name = row.get("data", {}).get("name") or ""
        if query_lower and query_lower not in name.lower():
            continue
        collection_meta = row.get("meta", {})
        collections.append({"key": row["key"], "name": name,
                            "parent": row.get("data", {}).get("parentCollection") or None,
                            "item_count": collection_meta.get("numItems"),
                            "subcollection_count": collection_meta.get("numCollections"),
                            "path": path_of(row)})
    collections.sort(key=lambda collection: collection["path"].lower())
    return {"meta": {"operation": "zotero_collections", "count": len(collections)}, "results": collections}


def zotero_get(keys, *, children: bool = False) -> dict:
    """Fetch the complete data for specific item(s) by key — every field, optionally with child items.

    `keys` is a single 8-char item key or a list of them. Each fetched record is {"key", "data": <full
    editable JSON: title, creators, all bib fields, tags, collections, ...>, "zotero_meta": <Zotero's item
    meta, e.g. numChildren>}; with `children=True` it also carries that item's child notes/attachments under
    "children". A scalar key returns {"meta": {operation}, "result": <record>}; a list returns
    {"meta": {operation, batch, count, ok, failed}, "results": [<record or {"_error","input"}>, ...]}
    fetched in parallel. Use `zotero_items` to discover keys (full list or quicksearch).
    """
    _, base = _zotero_config()

    def fetch(key):
        response = _zotero_get(f"{base}/items/{key}")
        response.raise_for_status()
        payload = response.json()
        record = {"key": key, "data": payload.get("data", {}), "zotero_meta": payload.get("meta", {})}
        if children:
            child_response = _zotero_get(f"{base}/items/{key}/children")
            child_response.raise_for_status()
            record["children"] = child_response.json()
        return record

    if isinstance(keys, (list, tuple)):
        def safe(key):
            try:
                return fetch(key)
            except Exception as error:  # noqa: BLE001
                return {"_error": f"{type(error).__name__}: {error}", "input": key}

        return _batch_summary("zotero_get", _parallel_map(safe, list(keys)))
    return {"meta": {"operation": "zotero_get"}, "result": fetch(keys)}


def _zotero_existing_doi_map() -> dict[str, str]:
    """Map every DOI already in the library to its item key (lowercased), for dedup."""
    return {item["doi"]: item["key"] for item in zotero_items()["results"] if item.get("doi")}


def _zotero_upload_file(attachment_key: str, pdf_path: str) -> dict:
    """Upload a local PDF's bytes to an existing attachment item: authorize, store, register.

    The file endpoints are guarded by If-None-Match (file presence), not the library version, so these
    steps are safe to run in parallel across many attachments without version-conflict races.
    """
    _, base = _zotero_config()
    path = pathlib.Path(pdf_path)
    blob = path.read_bytes()
    md5 = hashlib.md5(blob).hexdigest()
    filename, filesize, mtime = path.name, len(blob), int(path.stat().st_mtime * 1000)

    auth_response = _zotero_request(
        "POST", f"{base}/items/{attachment_key}/file",
        headers={"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"},
        content=urllib.parse.urlencode({"md5": md5, "filename": filename,
                                        "filesize": filesize, "mtime": mtime}),
    )
    auth_response.raise_for_status()
    auth = auth_response.json()
    if auth.get("exists"):  # Identical file already on the server; nothing more to upload.
        return {"ok": True, "key": attachment_key, "existed": True}

    body = auth["prefix"].encode() + blob + auth["suffix"].encode()
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
        upload = client.post(auth["url"], headers={"Content-Type": auth["contentType"]}, content=body)
    upload.raise_for_status()

    register = _zotero_request(
        "POST", f"{base}/items/{attachment_key}/file",
        headers={"Content-Type": "application/x-www-form-urlencoded", "If-None-Match": "*"},
        content=urllib.parse.urlencode({"upload": auth["uploadKey"]}),
    )
    register.raise_for_status()
    return {"ok": True, "key": attachment_key, "existed": False}


def zotero_attach(attachments) -> dict:
    """Attach PDF(s) to existing items as imported_file children.

    Accepts a single attachment dict or a list; each is {"parent_key": <item key>, "pdf_path": <local PDF>,
    "title"?: <attachment title>}. The attachment items are created in a single batched write first (so their
    version-guarded creation cannot race), then the file bytes are uploaded in parallel. Returns the unified
    envelope {"meta": {operation, count, ok, failed}, "results": [...]} with one {ok, parent_key, key, ...}
    per attachment, errors captured per item.
    """
    attachments = attachments if isinstance(attachments, list) else [attachments]
    if not attachments:
        return {"meta": {"operation": "zotero_attach", "count": 0, "ok": 0, "failed": 0}, "results": []}

    templates = []
    for attachment in attachments:
        path = pathlib.Path(attachment["pdf_path"])
        templates.append({"itemType": "attachment", "parentItem": attachment["parent_key"],
                          "linkMode": "imported_file", "title": attachment.get("title") or path.name,
                          "contentType": "application/pdf", "filename": path.name, "note": "",
                          "charset": "", "tags": [], "relations": {}, "md5": None, "mtime": None})
    created = _zotero_post_items(templates)
    index_to_key = {entry["index"]: entry["key"] for entry in created["success"]}
    failures = {entry["index"]: entry for entry in created["failed"]}

    def upload(index):
        attachment = attachments[index]
        attachment_key = index_to_key.get(index)
        if not attachment_key:
            return {"ok": False, "parent_key": attachment.get("parent_key"),
                    "error": failures.get(index, "attachment item not created")}
        try:
            result = _zotero_upload_file(attachment_key, attachment["pdf_path"])
            result["parent_key"] = attachment.get("parent_key")
            return result
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "parent_key": attachment.get("parent_key"), "key": attachment_key,
                    "error": f"{type(error).__name__}: {error}"}

    results = _parallel_map(upload, list(range(len(attachments))))
    ok = sum(1 for outcome in results if outcome.get("ok"))
    return {"meta": {"operation": "zotero_attach", "count": len(results), "ok": ok,
                     "failed": len(results) - ok}, "results": results}


_JATS_TAG = re.compile(r"<[^>]+>")


def _strip_jats(text: str | None) -> str:
    """Strip the JATS/HTML tags CrossRef wraps abstracts in, leaving plain text."""
    return _JATS_TAG.sub("", text).strip() if text else ""


def _creators_from_names(names: list[str]) -> list[dict]:
    """Best-effort split of display-name strings into Zotero author creators.

    'Family, Given' is split on the comma; otherwise the last whitespace token is taken as the family
    name. A fallback only — structured CrossRef authors are preferred when available.
    """
    creators = []
    for name in names:
        if not name:
            continue
        if "," in name:
            family, _, given = name.partition(",")
            creators.append({"creatorType": "author", "firstName": given.strip(), "lastName": family.strip()})
        else:
            parts = name.split()
            if len(parts) > 1:
                creators.append({"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
            else:
                creators.append({"creatorType": "author", "name": name})
    return creators


def _crossref_item(doi: str) -> dict:
    """Fetch one CrossRef record and shape the journal-article fields Zotero needs (creators, bib, abstract)."""
    response = _http_get(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}",
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"})
    response.raise_for_status()
    message = response.json()["message"]
    creators = []
    for author in message.get("author", []):
        if author.get("given") or author.get("family"):
            creators.append({"creatorType": "author", "firstName": author.get("given", ""),
                             "lastName": author.get("family", "")})
        elif author.get("name"):
            creators.append({"creatorType": "author", "name": author["name"]})
    issued = (message.get("issued") or {}).get("date-parts") or [[None]]
    return {
        "title": (message.get("title") or [None])[0],
        "creators": creators,
        "container": (message.get("container-title") or [None])[0],
        "volume": message.get("volume", ""), "issue": message.get("issue", ""),
        "pages": message.get("page", ""), "issn": (message.get("ISSN") or [None])[0],
        "date": str(issued[0][0]) if issued and issued[0] and issued[0][0] else "",
        "abstract": _strip_jats(message.get("abstract")), "language": message.get("language", ""),
    }


def _zotero_journal_item(record: dict, crossref: dict | None, tags, collections) -> dict:
    """Build a complete journalArticle editable-JSON item from a normalized record plus CrossRef bib data."""
    crossref = crossref or {}
    identifiers = record.get("ids") or {}
    doi = identifiers.get("doi")
    creators = crossref.get("creators") or _creators_from_names(record.get("authors") or [])
    return {
        "itemType": "journalArticle",
        "title": crossref.get("title") or record.get("title") or "",
        "creators": creators,
        "date": crossref.get("date") or (str(record.get("year")) if record.get("year") else ""),
        "publicationTitle": crossref.get("container") or record.get("venue") or "",
        "volume": crossref.get("volume", ""), "issue": crossref.get("issue", ""),
        "pages": crossref.get("pages", ""), "ISSN": crossref.get("issn") or "",
        "DOI": doi or "", "url": record.get("url") or (f"https://doi.org/{doi}" if doi else ""),
        "abstractNote": crossref.get("abstract") or record.get("abstract") or "",
        "language": crossref.get("language", "") or "",
        "tags": [{"tag": tag} for tag in (tags or [])],
        "collections": list(collections or []), "relations": {},
    }


def zotero_save(papers, *, collections=None, tags=None, dedup=True,
                download=True, scihub_proxy=None, email=None) -> dict:
    """One-call batched pipeline: take papers → dedup → fetch metadata + PDFs in parallel → create + attach.

    `papers` is a list of paper ids (DOI / arXiv / PMID / OpenAlex id) and/or already-normalized records
    (dicts carrying an "ids" object, e.g. straight from search()/author_works()). The pipeline:
      1. Resolves any bare ids to records (parallel).
      2. Dedups by DOI against the existing library (skip when `dedup`), so re-running is safe.
      3. Fetches CrossRef bib fields and downloads the best PDF for each new paper (parallel; PDF only
         when `download`, walking the open-access ladder then Sci-Hub).
      4. Creates the journalArticle items in batches of 50 (full citation: every field populated).
      5. Uploads each downloaded PDF as an attachment in parallel.
    `collections` is a list of existing collection KEYS; `tags` a list of tag strings applied to every item.
    Returns a summary with created keys, skipped duplicates, attachment outcomes, and any per-paper errors.
    """
    _author_email(email)
    papers = papers if isinstance(papers, list) else [papers]

    # 1) Resolve every input to a normalized record (in parallel for bare ids).
    def to_record(paper):
        if isinstance(paper, dict) and paper.get("ids"):
            return paper
        try:
            _, record = _resolve_work(paper if isinstance(paper, str) else str(paper))
            return record
        except Exception as error:  # noqa: BLE001
            return {"_error": f"{type(error).__name__}: {error}", "input": paper}

    records = _parallel_map(to_record, list(papers))
    resolved = [record for record in records if not (isinstance(record, dict) and record.get("_error"))]
    errors = [record for record in records if isinstance(record, dict) and record.get("_error")]

    # 2) Dedup by DOI against the existing library.
    skipped: list[dict] = []
    if dedup:
        existing = _zotero_existing_doi_map()
        fresh = []
        for record in resolved:
            doi = (record.get("ids") or {}).get("doi")
            if doi and doi in existing:
                skipped.append({"doi": doi, "key": existing[doi], "title": record.get("title")})
            else:
                fresh.append(record)
        resolved = fresh

    if not resolved:
        return {"meta": {"operation": "zotero_save", "created": 0, "skipped": len(skipped),
                         "errors": len(errors)},
                "created": [], "skipped": skipped, "attachments": [], "errors": errors}

    # 3) Fetch CrossRef bib data and download PDFs for each new paper, in parallel.
    def enrich(record):
        doi = (record.get("ids") or {}).get("doi")
        crossref = None
        if doi:
            try:
                crossref = _crossref_item(doi)
            except Exception as error:  # noqa: BLE001
                logger.warning("CrossRef lookup failed for %s: %s", doi, error)
        pdf_path = None
        if download:
            try:
                routes = _resolve_fulltext_routes(record, email)
                pdf_bytes, _ = _acquire_pdf(record, routes, scihub_proxy)
                if pdf_bytes:
                    stem = (doi or (record.get("ids") or {}).get("arxiv") or "paper").replace("/", "_")
                    target = pathlib.Path(tempfile.gettempdir()) / f"zotero_{stem}.pdf"
                    target.write_bytes(pdf_bytes)
                    pdf_path = str(target)
            except Exception as error:  # noqa: BLE001
                logger.warning("PDF acquisition failed for %s: %s", doi, error)
        return {"record": record, "crossref": crossref, "pdf_path": pdf_path}

    enriched = _parallel_map(enrich, resolved)

    # 4) Create all items in batches of 50.
    item_payloads = [_zotero_journal_item(entry["record"], entry["crossref"], tags, collections)
                     for entry in enriched]
    creation = _zotero_post_items(item_payloads)
    index_to_key = {entry["index"]: entry["key"] for entry in creation["success"]}

    created = []
    attach_jobs = []
    for index, enriched_record in enumerate(enriched):
        key = index_to_key.get(index)
        identifiers = enriched_record["record"].get("ids") or {}
        created.append({"index": index, "key": key, "doi": identifiers.get("doi"),
                        "title": enriched_record["record"].get("title"),
                        "has_pdf": bool(enriched_record["pdf_path"])})
        if key and enriched_record["pdf_path"]:
            attach_jobs.append({"parent_key": key, "pdf_path": enriched_record["pdf_path"]})

    # 5) Upload PDFs in parallel.
    attachments = zotero_attach(attach_jobs)["results"] if attach_jobs else []

    if creation["failed"]:
        logger.warning("zotero_save: %d item(s) failed to create: %s",
                       len(creation["failed"]), creation["failed"])
    return {
        "meta": {"operation": "zotero_save", "created": len(index_to_key),
                 "skipped": len(skipped), "attached": sum(1 for outcome in attachments if outcome.get("ok")),
                 "failed": len(creation["failed"]), "errors": len(errors)},
        "created": created, "skipped": skipped, "attachments": attachments,
        "create_failures": creation["failed"], "errors": errors,
        "library_version": creation["library_version"],
    }


# Each public per-item function gains list-input fan-out: pass a list and it runs in parallel.
