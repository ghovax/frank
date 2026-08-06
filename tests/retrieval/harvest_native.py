"""Record corpora from live macOS applications. Run deliberately, never from a test."""

from __future__ import annotations

import logging

from langmesh.computer.engine import NativeSurface

from tests.retrieval.corpus import Corpus, RecordedElement, write_corpus

logger = logging.getLogger(__name__)

# Applications worth recording if they are running: a file manager, a browser chrome, a terminal, a settings window, an editor and a document viewer cover most of the shapes a native tree takes.
APPLICATIONS_TO_HARVEST = (
    "Finder", "Photos", "Terminal", "System Settings", "Code", "RStudio", "Claude", "Skim",
    "Reminders", "Anki",
)

# Attributes surveyed across 921 live elements and deliberately not recorded.

def harvest_application(surface: NativeSurface, application_name: str) -> Corpus | None:
    """Record one running application, or ``None`` if it is not running or has nothing to read."""
    result = surface.documents(application_name)
    if not result.get("ok") or not result.get("documents"):
        return None
    documents = result["documents"]
    by_id = {document.id: document for document in documents}

    def ancestor_path(document) -> str:
        """The chain of ancestor roles, outermost first — the native analogue of a DOM path."""
        steps, current, guard = [], by_id.get(document.parent), 0
        while current is not None and guard < 6:
            role = str(current.payload.get("role") or "").replace("AX", "")
            if role:
                steps.append(role)
            current = by_id.get(current.parent)
            guard += 1
        return " > ".join(reversed(steps))

    recorded = tuple(
        RecordedElement(
            role=str(document.payload.get("role") or ""),
            name=str(document.payload.get("name") or ""),
            value=str(document.payload.get("value") or ""),
            context=str(document.payload.get("context") or ""),
            placeholder=str(document.payload.get("placeholder") or ""),
            role_description=str(document.payload.get("role_description") or ""),
            subrole=str(document.payload.get("subrole") or ""),
            path=ancestor_path(document),
        )
        for document in documents
    )
    return Corpus(site_name=application_name.replace(" ", "-").lower(),
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
        with_path = sum(1 for element in corpus.elements if element.path)
        logger.info("%-18s %5d elements  %4d value  %4d ancestor path  -> %s",
                    application_name, len(corpus), with_value, with_path, destination.name)
        recorded_any = True
    return 0 if recorded_any else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
