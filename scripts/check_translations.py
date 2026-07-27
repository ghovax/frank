"""Keep the message catalogues honest: same keys everywhere, and every key actually used.

Two failures this catches, both of which happened.

A catalogue that has drifted renders a raw key path to whoever reads that language, and
nothing else notices — `next-intl` resolves at run time, so a missing Japanese key is a
Japanese-only defect that an English-speaking developer will never see. Comparing the key
sets is the only cheap way to know.

An orphaned key is the residue of deleting a feature. It is harmless to render and expensive
to read: the next person to touch the catalogue cannot tell which strings are live, so they
translate, review and maintain copy for surfaces that no longer exist. Deleting the artifact
surface orphaned twenty-nine of them at once, which is what this script was written for.

Usage detection is deliberately crude — a quoted occurrence of the key anywhere under
`web/src` counts. That is a *false-negative* bias: a key referenced only through a computed
string would be reported as unused, so if that ever becomes a real pattern the fix is to
name the key literally somewhere, not to loosen the check. Erring the other way would let
the residue back in, which is the thing being prevented.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CATALOGUES = WEB / "messages"
SOURCE = WEB / "src"


def flatten(catalogue: dict, prefix: str = "") -> dict[str, str]:
    """Every leaf as a dotted path, so a nested section compares like a flat one."""
    flat: dict[str, str] = {}
    for key, value in catalogue.items():
        if isinstance(value, dict):
            flat.update(flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def main() -> int:
    catalogues = {path.stem: flatten(json.loads(path.read_text())) for path in sorted(CATALOGUES.glob("*.json"))}
    if "en" not in catalogues:
        print("check_translations: no en.json to compare against", file=sys.stderr)
        return 1

    failures: list[str] = []

    reference = set(catalogues["en"])
    for language, catalogue in catalogues.items():
        if language == "en":
            continue
        missing = sorted(reference - set(catalogue))
        extra = sorted(set(catalogue) - reference)
        for key in missing:
            failures.append(f"{language}.json is missing {key}")
        for key in extra:
            failures.append(f"{language}.json has {key}, which en.json does not")

    blob = "\n".join(
        path.read_text() for path in SOURCE.rglob("*") if path.suffix in (".ts", ".tsx")
    )
    for dotted in sorted(reference):
        leaf = dotted.rsplit(".", 1)[-1]
        if not re.search(rf"""["'`]{re.escape(leaf)}["'`]""", blob):
            failures.append(f"{dotted} is in the catalogues but nothing under web/src uses it")

    if failures:
        for failure in failures:
            print(f"check_translations: {failure}", file=sys.stderr)
        return 1
    print(f"translations ok ({len(reference)} keys × {len(catalogues)} languages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
