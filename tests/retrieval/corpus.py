"""Recorded corpora of real web-page elements, and the types that carry them."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class RecordedElement:
    """One element of a page, holding every field any encoding strategy might want."""

    role: str
    name: str
    value: str = ""
    context: str = ""
    url: str = ""
    title: str = ""
    subrole: str = ""
    placeholder: str = ""
    role_description: str = ""
    source: str = ""
    """What the surface itself published, verbatim and unprocessed: an element's markup on the
    web. Empty on a native window, which has no source text behind it.

    It held a *synthesised* record on the native side until this was noticed — a string built at
    harvest time by pasting `role`, `name` and `value` back together. That is stored derivation,
    not stored data: it carried no information the other columns did not already have, it could
    not be reassembled a different way without re-recording every application, and every
    measurement taken against it was comparing the same fields to themselves in an odd format.
    A fixture records what a surface said. Anything built out of that belongs to analysis, where
    it can be rebuilt for free and questioned afterwards."""
    path: str = ""
    """The structural route to the element: ``nav > ul > li > a`` on the web, a chain of ancestor
    roles on a native window. Structure the flat fields cannot express."""


@dataclass(frozen=True)
class Corpus:
    """Every element recorded from one surface, with where it was read from."""

    site_name: str
    page_url: str
    elements: tuple[RecordedElement, ...]
    surface_name: str = "web"

    def __len__(self) -> int:
        return len(self.elements)


def fixture_path(surface_name: str, site_name: str) -> Path:
    """Where the fixture for one recording lives: a directory per surface, named by the source."""
    return FIXTURE_DIRECTORY / surface_name / f"{site_name}.json"


def write_corpus(corpus: Corpus) -> Path:
    """Write one corpus to its fixture file, creating its surface's directory if needed."""
    destination = fixture_path(corpus.surface_name, corpus.site_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "site_name": corpus.site_name,
        "surface_name": corpus.surface_name,
        "page_url": corpus.page_url,
        "elements": [asdict(element) for element in corpus.elements],
    }
    destination.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
    return destination


def read_corpus(path: Path) -> Corpus:
    """Read one corpus back from its fixture file."""
    document = json.loads(path.read_text())
    return Corpus(
        site_name=document["site_name"],
        surface_name=document.get("surface_name", "web"),
        page_url=document["page_url"],
        elements=tuple(RecordedElement(**element) for element in document["elements"]),
    )


def load_all_corpora(surface_name: str | None = None) -> list[Corpus]:
    """Every committed corpus, ordered by site name so that reports are stable between runs."""
    if not FIXTURE_DIRECTORY.exists():
        return []
    pattern = f"{surface_name}/*.json" if surface_name else "*/*.json"
    return [read_corpus(path) for path in sorted(FIXTURE_DIRECTORY.glob(pattern))]
