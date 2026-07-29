"""Recorded corpora of real web-page elements, and the types that carry them.

A corpus is a snapshot of one real page, read through frank's own snapshot parser and written to
a JSON fixture. The fixtures are committed and the tests read only those, never the network. A
test that reaches eight websites is slow, fails whenever a site is down, and quietly reports
different numbers every week as those sites are redesigned — which is indistinguishable from a
regression in our own code, and therefore worse than useless as a signal.

Refreshing the fixtures is a deliberate, separate act: run :mod:`tests.retrieval.harvest`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class RecordedElement:
    """One element of a page, holding every field any encoding strategy might want.

    The fields are stored even when the strategy currently shipping ignores them, because the
    point of the harness is to compare what we do against what we could do. A fixture that only
    recorded the fields already in use could never demonstrate that a different choice is better.
    """

    role: str
    name: str
    value: str = ""
    context: str = ""
    url: str = ""
    title: str = ""


@dataclass(frozen=True)
class Corpus:
    """Every element recorded from one surface, with where it was read from.

    ``surface_name`` separates the two, because they are different retrieval problems and must
    not be pooled: a web page supplies link destinations and tooltips that no native window has,
    so a strategy scored across both would be credited on one surface for a field the other
    cannot provide."""

    site_name: str
    page_url: str
    elements: tuple[RecordedElement, ...]
    surface_name: str = "web"

    def __len__(self) -> int:
        return len(self.elements)


def fixture_path(site_name: str) -> Path:
    """Where the fixture for one site lives."""
    return FIXTURE_DIRECTORY / f"{site_name}.json"


def write_corpus(corpus: Corpus) -> Path:
    """Write one corpus to its fixture file, creating the fixture directory if needed."""
    FIXTURE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    destination = fixture_path(corpus.site_name)
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
    """Every committed corpus, ordered by site name so that reports are stable between runs.

    Pass ``surface_name`` to take only one surface's corpora — the usual case, since pooling web
    and native measures neither."""
    if not FIXTURE_DIRECTORY.exists():
        return []
    corpora = [read_corpus(path) for path in sorted(FIXTURE_DIRECTORY.glob("*.json"))]
    if surface_name is None:
        return corpora
    return [corpus for corpus in corpora if corpus.surface_name == surface_name]
