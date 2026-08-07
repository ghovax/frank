"""Fetching a URL and downloading a file, as plain functions the runtime dispatches to."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import minify_html
from bs4 import BeautifulSoup
from markdownify import markdownify as _markdownify

from langmesh.base.tuning import Tunable, active_tuning, clip_to_tokens
from langmesh.locations.executor import LocationExecutor
from langmesh.base.serialization import compact
from langmesh.runtime.tools import context as tool_context

#: The formats a caller may ask for, the first being what an unrecognised one falls back to.
_FORMATS = ("markdown", "text", "html")

#: Elements whose contents are code or presentation, which carry nothing for a reader.
_UNREADABLE = ("script", "style", "noscript", "template")

#: Elements that end a line of prose, so text taken across them does not run together.
_BLOCKS = (
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "nav",
    "aside",
    "main",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "tr",
    "br",
    "pre",
    "blockquote",
    "table",
)


def _payload(code: str, **fields) -> str:
    """Build a JSON tool-result payload with the given ``code`` discriminator."""
    return compact({"code": code, **fields})


def _http_url(url: str) -> str:
    """The URL, once it is one this can actually fetch."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("The URL must be a fully-formed http(s) URL.")
    return url


async def fetch_url(url: str, output_format: str = "markdown", timeout_seconds: int = 30) -> str:
    """Fetch a URL in the requested format, through a cascade of engines each better at defeating a wall than the last."""
    output_format = (output_format or "").lower()
    if output_format not in _FORMATS:
        output_format = _FORMATS[0]
    content, engine = await _fetch_through_engines(_http_url(url), output_format, timeout_seconds)

    inline_content, truncated = clip_to_tokens(
        content, active_tuning().amount(Tunable.fetch_tokens)
    )
    fields: dict[str, object] = {
        "url": url,
        "format": output_format,
        "engine": engine,
        "truncated": truncated,
    }
    if truncated:
        # The whole page goes to scratch, since what was clipped is what the reader most likely wants next.
        output_path = tool_context.current().spill_path("fetch")
        output_path.write_text(content)
        fields["output_file"] = str(output_path)
        fields["size"] = len(content)
    fields["content"] = inline_content
    return _payload("fetch_completed", **fields)


def _fetch_engines():
    """The fetch engines to try, in order, with Firecrawl offered only when a client is configured."""
    yield ("jina", _fetch_via_jina)
    if tool_context.current().firecrawl_client is not None:
        yield ("firecrawl", _fetch_via_firecrawl)
    yield ("direct", _fetch_direct)


async def _fetch_through_engines(
    url: str, output_format: str, timeout_seconds: int
) -> tuple[str, str]:
    """Walk the engine cascade, returning the first substantial result, the longest thin one, or a combined error."""
    # Below this a page is a wall or a stub rather than the content, so the next engine is worth trying.
    minimum_useful_characters = tool_context.current().minimum_useful_characters
    best_content = ""
    best_engine = ""
    failures: list[str] = []
    for engine, fetcher in _fetch_engines():
        try:
            content = await fetcher(url, output_format, timeout_seconds)
        except Exception as error:  # noqa: BLE001 — any engine failure just falls through
            failures.append(f"{engine}: {error}")
            continue
        if len(content.strip()) >= minimum_useful_characters:
            return content, engine
        if len(content) > len(best_content):
            best_content, best_engine = content, engine
    if best_content:
        return best_content, best_engine
    raise RuntimeError("Could not fetch the URL. " + "; ".join(failures))


async def _fetch_via_jina(url: str, output_format: str, timeout_seconds: int) -> str:
    """Fetch through Jina Reader, which works keyless and takes a key only to raise the rate limit."""
    import httpx

    # Jina names its return formats exactly as this does, so the requested one is the header's value.
    headers = {"X-Return-Format": output_format}
    jina_api_key = tool_context.current().jina_api_key
    if jina_api_key:
        headers["Authorization"] = f"Bearer {jina_api_key}"
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(f"https://r.jina.ai/{url}", headers=headers)
        response.raise_for_status()
        return response.text


async def _fetch_via_firecrawl(url: str, output_format: str, timeout_seconds: int) -> str:
    """Fetch through Firecrawl's full-browser scrape, returning HTML or its clean markdown."""
    scrape_format = "html" if output_format == "html" else "markdown"
    client = tool_context.current().firecrawl_client
    document = await client.scrape(url, formats=[scrape_format], timeout=timeout_seconds * 1000)
    content = document.html if output_format == "html" else document.markdown
    return content or ""


async def _impersonated_get(url: str, timeout_seconds: int):
    """A GET that mimics a real Chrome down to the TLS fingerprint, routed through the configured proxy."""
    from curl_cffi import AsyncSession

    session_arguments: dict[str, object] = {"impersonate": "chrome", "timeout": timeout_seconds}
    proxy_url = tool_context.current().proxy_url
    if proxy_url:
        session_arguments["proxies"] = {"http": proxy_url, "https": proxy_url}
    async with AsyncSession(**session_arguments) as session:
        response = await session.get(url)
        response.raise_for_status()
        return response


async def _fetch_direct(url: str, output_format: str, timeout_seconds: int) -> str:
    """Last-resort direct fetch with local conversion, for when the scraping services are unset or unreachable."""
    response = await _impersonated_get(url, timeout_seconds)
    body = response.text
    if output_format == "html":
        return _minified(body)
    if output_format == "text":
        return _text_from_html(body)
    return _markdownify(body)


async def download_file(
    executor: LocationExecutor,
    url: str,
    resolved_path: str,
    timeout_seconds: int = 120,
) -> str:
    """Download a URL's bytes and write them through the executor, so the file lands on the target location."""
    response = await _impersonated_get(_http_url(url), timeout_seconds)
    data = response.content
    content_type = response.headers.get("content-type", "")
    await asyncio.to_thread(executor.write_bytes, resolved_path, data)
    return _payload(
        "download_completed",
        url=url,
        path=resolved_path,
        bytes=len(data),
        content_type=content_type,
    )


def _minified(html: str) -> str:
    """The page with its comments, redundant whitespace and inline assets squeezed out, none of which a reader needs."""
    try:
        return minify_html.minify(
            html, minify_css=True, minify_js=True, remove_processing_instructions=True
        )
    except Exception:  # noqa: BLE001 — a page too malformed to minify is still a page worth returning
        return html


def _text_from_html(html: str) -> str:
    """The page's readable text, taken with a parser rather than a pattern, since HTML is not a regular language."""
    page = BeautifulSoup(html, "html.parser")
    for element in page(_UNREADABLE):
        element.decompose()
    # A newline per block, so a paragraph ends where it ends and an inline run stays one line.
    for element in page(_BLOCKS):
        element.append("\n")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", page.get_text())).strip()


__all__ = ["fetch_url", "download_file"]
