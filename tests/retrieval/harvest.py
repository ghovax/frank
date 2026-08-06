"""Refresh the committed corpora from live websites. Run deliberately, never from a test."""

from __future__ import annotations

import logging

from langmesh.computer.web import _folded_label, _parse_snapshot, _snapshot, _titles_by_label

from tests.retrieval.corpus import Corpus, RecordedElement, write_corpus

# Chosen for variety of page shape rather than familiarity: an encyclopaedia article dense with links, a plain-HTML link list, a web application, developer documentation, a marketing home page, a news front page, a research listing, and a government site.
PAGES_TO_HARVEST = {
    "wikipedia": "https://en.wikipedia.org/wiki/Accessibility",
    "hackernews": "https://news.ycombinator.com/",
    "github": "https://github.com/microsoft/playwright",
    "mdn": "https://developer.mozilla.org/en-US/docs/Web/Accessibility",
    "python": "https://www.python.org/",
    "bbc": "https://www.bbc.com/news",
    "arxiv": "https://arxiv.org/list/cs.HC/recent",
    "nps": "https://www.nps.gov/index.htm",
}

logger = logging.getLogger(__name__)

# One pass over the DOM collecting what the accessibility snapshot cannot: an element's own markup, its opening tag, its attributes, and its structural path.
_DECLARATIONS_BY_LABEL = r"""() => {
  const interesting = 'a,button,input,select,textarea,summary,label,option,[role],[onclick],[tabindex]';
  const declarations = new Map();
  const ambiguous = new Set();
  const pathOf = (node) => {
    const steps = [];
    let current = node;
    while (current && current.nodeType === 1 && steps.length < 6) {
      steps.push(current.tagName.toLowerCase());
      current = current.parentElement;
    }
    return steps.reverse().join(' > ');
  };
  for (const node of document.querySelectorAll(interesting)) {
    const label = (node.innerText || node.getAttribute('aria-label') || node.getAttribute('alt') || '')
      .trim().replace(/\s+/g, ' ').slice(0, 120);
    if (!label) continue;
    const markup = (node.outerHTML || '').replace(/\s+/g, ' ').slice(0, 600);
    const openingTag = markup.slice(0, markup.indexOf('>') + 1);
    const attributes = Array.from(node.attributes || [])
      .map((attribute) => `${attribute.name}="${attribute.value}"`).join(' ').slice(0, 400);
    const declaration = { markup, openingTag, attributes, path: pathOf(node) };
    const seen = declarations.get(label);
    if (seen && seen.markup !== declaration.markup) { ambiguous.add(label); continue; }
    declarations.set(label, declaration);
  }
  for (const label of ambiguous) declarations.delete(label);
  return Object.fromEntries(declarations);
}"""

PAGE_SETTLE_MILLISECONDS = 1500
NAVIGATION_TIMEOUT_MILLISECONDS = 45_000


def harvest_page(page, site_name: str, page_url: str) -> Corpus:
    """Read one page into a corpus, using the product's parser and tooltip reader."""
    page.goto(page_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MILLISECONDS)
    page.wait_for_timeout(PAGE_SETTLE_MILLISECONDS)
    parsed_elements, _frame_owners = _parse_snapshot(_snapshot(page))
    tooltips = _titles_by_label(page)
    declarations = page.evaluate(_DECLARATIONS_BY_LABEL)
    by_label = {_folded_label(label): value for label, value in (declarations or {}).items()}
    recorded = []
    for element in parsed_elements:
        declaration = by_label.get(_folded_label(element.name), {})
        recorded.append(RecordedElement(
            role=element.role,
            name=element.name,
            value=element.value if isinstance(element.value, str) else "",
            context=element.context,
            url=str(element.flags.get("url") or ""),
            title=tooltips.get(_folded_label(element.name), ""),
            source=str(declaration.get("markup", "")),
            path=str(declaration.get("path", "")),
        ))
    recorded = tuple(recorded)
    return Corpus(site_name=site_name, page_url=page_url, elements=recorded)


def main() -> int:
    from playwright.sync_api import sync_playwright

    failures = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for site_name, page_url in PAGES_TO_HARVEST.items():
            page = browser.new_page()
            try:
                corpus = harvest_page(page, site_name, page_url)
                destination = write_corpus(corpus)
                with_url = sum(1 for element in corpus.elements if element.url)
                with_title = sum(1 for element in corpus.elements if element.title)
                with_source = sum(1 for element in corpus.elements if element.source)
                logger.info("%-11s %5d elements  %4d url  %4d tooltip  %4d markup  -> %s",
                            site_name, len(corpus), with_url, with_title, with_source, destination.name)
            except Exception as error:  # noqa: BLE001 — one unreachable site must not stop the rest
                failures += 1
                logger.error("%-11s failed: %s: %s", site_name, type(error).__name__, error)
            finally:
                page.close()
        browser.close()
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
