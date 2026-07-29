"""Refresh the committed corpora from live websites. Run deliberately, never from a test.

    uv run python -m tests.retrieval.harvest

This reads each page through frank's own snapshot parser and tooltip reader, so a fixture is a
recording of what the product actually sees rather than of what a separate scraper thinks a page
contains. When the parser improves, re-harvesting is how the fixtures catch up.

The pages are public, read-only, and opened in a headless browser of our own. Nothing here
attaches to a browser the user is using, and nothing clicks, submits, or otherwise touches a page.
"""

from __future__ import annotations

import logging

from frank.computer.web import _folded_label, _parse_snapshot, _snapshot, _titles_by_label

from tests.retrieval.corpus import Corpus, RecordedElement, write_corpus

# Chosen for variety of page shape rather than familiarity: an encyclopaedia article dense with
# links, a plain-HTML link list, a web application, developer documentation, a marketing home
# page, a news front page, a research listing, and a government site.
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

PAGE_SETTLE_MILLISECONDS = 1500
NAVIGATION_TIMEOUT_MILLISECONDS = 45_000


def harvest_page(page, site_name: str, page_url: str) -> Corpus:
    """Read one page into a corpus, using the product's parser and tooltip reader."""
    page.goto(page_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MILLISECONDS)
    page.wait_for_timeout(PAGE_SETTLE_MILLISECONDS)
    parsed_elements, _frame_owners = _parse_snapshot(_snapshot(page))
    tooltips = _titles_by_label(page)
    recorded = tuple(
        RecordedElement(
            role=element.role,
            name=element.name,
            value=element.value if isinstance(element.value, str) else "",
            context=element.context,
            url=str(element.flags.get("url") or ""),
            title=tooltips.get(_folded_label(element.name), ""),
        )
        for element in parsed_elements
    )
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
                logger.info("%-11s %5d elements  %4d with a url  %4d with a tooltip  -> %s",
                            site_name, len(corpus), with_url, with_title, destination.name)
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
