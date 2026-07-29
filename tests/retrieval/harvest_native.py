"""Record corpora from live macOS applications. Run deliberately, never from a test.

    uv run python -m tests.retrieval.harvest_native

Unlike the web harvester, what this captures depends on which applications happen to be running
and what they are showing, so a refresh will not reproduce the previous fixtures exactly. That is
a property of the surface rather than a flaw in the recording: a native window has no stable
public address the way a URL does. The fixtures are still worth committing, because a recording
of a real window beats a synthetic tree, and because the questions asked of them — does this field
help, does that one hurt — are answered within a corpus rather than across refreshes.

Read-only: this snapshots accessibility trees. It never clicks, types, or activates anything.
"""

from __future__ import annotations

import logging

from frank.computer.engine import NativeSurface

from tests.retrieval.corpus import Corpus, RecordedElement, write_corpus

logger = logging.getLogger(__name__)

# Applications worth recording if they are running: a file manager, a browser chrome, a terminal,
# a settings window, an editor and a document viewer cover most of the shapes a native tree takes.
APPLICATIONS_TO_HARVEST = (
    "Finder", "Photos", "Terminal", "System Settings", "Code", "RStudio", "Claude", "Skim",
    "Reminders", "Anki",
)

def harvest_application(surface: NativeSurface, application_name: str) -> Corpus | None:
    """Record one running application, or ``None`` if it is not running or has nothing to read.

    Read through :meth:`NativeSurface.documents` rather than the raw accessibility walk, because
    an element's *name* is derived — title, or failing that description, or failing that help —
    and that derivation is part of what is being measured. Recording the raw tree would measure a
    reimplementation of the product instead of the product."""
    result = surface.documents(application_name)
    if not result.get("ok") or not result.get("documents"):
        return None
    recorded = tuple(
        RecordedElement(
            role=str(document.payload.get("role") or ""),
            name=str(document.payload.get("name") or ""),
            value=str(document.payload.get("value") or ""),
            context=str(document.payload.get("context") or ""),
        )
        for document in result["documents"]
    )
    return Corpus(site_name=f"native-{application_name.replace(' ', '-').lower()}",
                  surface_name="native", page_url=f"application:{application_name}",
                  elements=recorded)


def main() -> int:
    surface = NativeSurface()
    recorded_any = False
    for application_name in APPLICATIONS_TO_HARVEST:
        try:
            corpus = harvest_application(surface, application_name)
        except Exception as error:  # noqa: BLE001 — one unreadable app must not stop the rest
            logger.error("%-18s failed: %s: %s", application_name, type(error).__name__, error)
            continue
        if corpus is None:
            logger.info("%-18s not running", application_name)
            continue
        if len(corpus) < 10:
            logger.info("%-18s only %d elements, too few to measure — skipped",
                        application_name, len(corpus))
            continue
        destination = write_corpus(corpus)
        with_value = sum(1 for element in corpus.elements if element.value)
        logger.info("%-18s %5d elements  %4d with a value  -> %s",
                    application_name, len(corpus), with_value, destination.name)
        recorded_any = True
    return 0 if recorded_any else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
