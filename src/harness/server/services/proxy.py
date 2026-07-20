"""A rewriting pass-through proxy for ``open_artifact`` of external URLs.

It serves the page — and *every* asset and request it makes — back through one route, so to the
framed page everything looks same-origin (our localhost). That is what lets sites that refuse
direct framing (``X-Frame-Options`` / ``frame-ancestors``) render, and avoids the cross-origin
CORS/history errors a naive ``<base>`` proxy hits. ES-module specifiers the browser resolves
itself (static import/export-from and string-literal dynamic import) are rewritten to absolute
proxied URLs, since relative ones would resolve against localhost and 404; a computed dynamic
``import()`` specifier cannot be rewritten statically and remains a known gap. One long-lived
client keeps the upstream cookie jar (session/consent/CSRF cookies, domain-scoped by httpx so
opened sites never share them) across proxied requests; framing-blocker and hop-by-hop
response headers are dropped when re-serving."""

from fastapi import Request
from harness.tools.tools import ASSETS_DIRECTORY
from typing import Any
from urllib.parse import quote
from urllib.parse import urljoin
from urllib.parse import urlparse
import httpx
import json
import re
from harness.server import state


_PROXY_PATH = "/artifact-proxy"


_PROXY_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


_PROXY_SKIP_SCHEMES = ("data:", "blob:", "javascript:", "mailto:", "tel:", "about:", "#", "vbscript:")


_PROXY_DROP_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "set-cookie",
    "strict-transport-security",
    "report-to",
    "reporting-endpoints",
}


_PROXY_DROP_REQUEST_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "content-length",
    "accept-encoding",
    "origin",
    "referer",
    "cookie",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
}


def _get_proxy_client() -> httpx.AsyncClient:
    if state._proxy_client is None:
        state._proxy_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers=_PROXY_BROWSER_HEADERS,
        )
    return state._proxy_client


_PROXY_HTML_ATTR_RE = re.compile(
    r'(?P<pre>\b(?:src|href|action|formaction|poster|data-src|data-href|data-url)\s*=\s*)'
    r'(?P<q>["\'])(?P<url>[^"\']*)(?P=q)',
    re.IGNORECASE,
)


_PROXY_HTML_SRCSET_RE = re.compile(r'(?P<pre>\bsrcset\s*=\s*)(?P<q>["\'])(?P<val>[^"\']*)(?P=q)', re.IGNORECASE)


_PROXY_STYLE_BLOCK_RE = re.compile(r'(<style[^>]*>)(?P<body>.*?)(</style>)', re.IGNORECASE | re.DOTALL)


_PROXY_CSS_URL_RE = re.compile(r'url\(\s*(?P<q>["\']?)(?P<url>[^)"\']+)(?P=q)\s*\)', re.IGNORECASE)


_PROXY_CSS_IMPORT_RE = re.compile(r'(?P<pre>@import\s+)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)', re.IGNORECASE)


_PROXY_CSP_META_RE = re.compile(r'<meta[^>]+http-equiv\s*=\s*["\']?content-security-policy[^>]*>', re.IGNORECASE)


_PROXY_BASE_TAG_RE = re.compile(r'<base[^>]*>', re.IGNORECASE)


_PROXY_STRIP_ATTR_RE = re.compile(
    r'\s+(?:integrity|crossorigin|nonce)(?:\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+))?',
    re.IGNORECASE,
)


def _proxy_ref(raw: str, base: str) -> str:
    """Resolve ``raw`` (possibly relative) against ``base`` and route it back through
    this proxy. Schemes that are not real fetches (data:, javascript:, #…) pass through."""
    target = raw.strip()
    if not target or target.lower().startswith(_PROXY_SKIP_SCHEMES):
        return raw
    absolute = urljoin(base, target)
    if not absolute.lower().startswith(("http://", "https://")):
        return raw
    return f"{_PROXY_PATH}?url={quote(absolute, safe='')}"


def _rewrite_proxy_css(text: str, base: str) -> str:
    text = _PROXY_CSS_URL_RE.sub(
        lambda match: f'url({match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")})', text
    )
    text = _PROXY_CSS_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', text
    )
    return text


_PROXY_JS_STATIC_IMPORT_RE = re.compile(
    r'(?P<pre>\b(?:import|export)\b[^;\n]*?\bfrom\s*)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)',
    re.IGNORECASE,
)


_PROXY_JS_BARE_IMPORT_RE = re.compile(
    r'(?P<pre>\bimport\s*)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)',
    re.IGNORECASE,
)


_PROXY_JS_DYNAMIC_IMPORT_RE = re.compile(
    r'(?P<pre>\bimport\s*\(\s*)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)(?P<post>\s*\))',
    re.IGNORECASE,
)


def _rewrite_proxy_js(text: str, base: str) -> str:
    """Rewrite ES-module import/export specifiers in a served script to proxied,
    absolute URLs. Applied to any response whose content type is JavaScript; the
    patterns only touch module syntax, which a classic script would not contain."""
    text = _PROXY_JS_STATIC_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', text
    )
    text = _PROXY_JS_DYNAMIC_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}{match.group("post")}', text
    )
    text = _PROXY_JS_BARE_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', text
    )
    return text


_PROXY_IMPORTMAP_RE = re.compile(
    r'(<script[^>]*\btype\s*=\s*["\']importmap["\'][^>]*>)(?P<body>.*?)(</script>)',
    re.IGNORECASE | re.DOTALL,
)


def _rewrite_importmap_urls(node: Any, base: str) -> Any:
    """Route every URL value in an import map (imports + scopes) through the proxy so
    bare specifiers the map resolves still load from the real origin."""
    if isinstance(node, dict):
        return {key: _rewrite_importmap_urls(value, base) for key, value in node.items()}
    if isinstance(node, str):
        return _proxy_ref(node, base)
    return node


def _rewrite_proxy_importmap(markup: str, base: str) -> str:
    def _replace(match: re.Match) -> str:
        try:
            parsed = json.loads(match.group("body"))
        except (ValueError, TypeError):
            return match.group(0)
        rewritten = json.dumps(_rewrite_importmap_urls(parsed, base))
        return f"{match.group(1)}{rewritten}{match.group(3)}"

    return _PROXY_IMPORTMAP_RE.sub(_replace, markup)


def _rewrite_proxy_srcset(value: str, base: str) -> str:
    rewritten = []
    for candidate in value.split(","):
        chunk = candidate.strip()
        if not chunk:
            continue
        bits = chunk.split(None, 1)
        descriptor = f" {bits[1]}" if len(bits) > 1 else ""
        rewritten.append(f"{_proxy_ref(bits[0], base)}{descriptor}")
    return ", ".join(rewritten)


_PROXY_RUNTIME_TEMPLATE = (ASSETS_DIRECTORY / "proxy_runtime.js").read_text(encoding="utf-8")


def _proxy_runtime(base: str) -> str:
    """A small shim injected into every proxied page so URLs built *by scripts*
    (fetch/XHR, history navigations, dynamically created elements) also go through
    the proxy and resolve against the real origin — not our localhost — and so a
    cross-origin ``history.replaceState`` no longer throws. The script itself lives
    in ``assets/proxy_runtime.js``; the per-page origin/prefix are substituted in."""
    source = (
        _PROXY_RUNTIME_TEMPLATE
        .replace("__DAISY_PROXY_BASE__", json.dumps(base))
        .replace("__DAISY_PROXY_URL__", json.dumps(f"{_PROXY_PATH}?url="))
        .replace("__DAISY_WS_PROXY_URL__", json.dumps("/artifact-proxy-ws?url="))
    )
    return f"""<script>
{source}
</script>"""


def _rewrite_proxy_html(markup: str, base: str) -> str:
    markup = _PROXY_CSP_META_RE.sub("", markup)
    markup = _PROXY_BASE_TAG_RE.sub("", markup)
    markup = _PROXY_STRIP_ATTR_RE.sub("", markup)
    # Import maps first — their JSON must be rewritten before the generic attribute
    # pass could disturb the <script> body.
    markup = _rewrite_proxy_importmap(markup, base)
    markup = _PROXY_HTML_ATTR_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', markup
    )
    markup = _PROXY_HTML_SRCSET_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_rewrite_proxy_srcset(match.group("val"), base)}{match.group("q")}', markup
    )
    markup = _PROXY_STYLE_BLOCK_RE.sub(
        lambda match: f'{match.group(1)}{_rewrite_proxy_css(match.group("body"), base)}{match.group(3)}', markup
    )
    runtime = _proxy_runtime(base)
    head_match = re.search(r"<head[^>]*>", markup, re.IGNORECASE)
    if head_match:
        return markup[: head_match.end()] + runtime + markup[head_match.end() :]
    return runtime + markup


def _proxy_forward_headers(request: Request, target_url: str) -> dict[str, str]:
    """The request headers to forward upstream: the browser's own headers (so the
    site sees a real browser) minus hop-by-hop/localhost-specific ones, with Origin
    and Referer rewritten to the target's own origin rather than our localhost frame
    (many APIs reject a mismatched Origin, or vary their response by Referer)."""
    forwarded = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _PROXY_DROP_REQUEST_HEADERS
    }
    parsed = urlparse(target_url)
    if parsed.scheme and parsed.netloc:
        forwarded["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        forwarded["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return forwarded
