"""Check the documentation against ASD-STE100 Simplified Technical English.

STE exists because technical writing is read by people who did not write it, often not in
their first language, sometimes while doing something else. It removes the choices that make
prose pleasant to write and hard to read: long sentences, buried subjects, a second word for a
thing that already has one.

This checks the rules that can be checked mechanically. It cannot check the dictionary — that
needs the ~900-word approved list, which is licensed and not redistributable — so it checks
structure, which is where most violations are anyway.

    uv run python scripts/check_simplified_english.py [paths...]

Exits non-zero when a rule is broken, so it can gate a commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ASD-STE100 Part 2. Instructions get 20 words; descriptive text gets 25. Prose in a README is
# descriptive, so 25 is the limit applied here.
MAX_SENTENCE_WORDS = 25
MAX_PARAGRAPH_SENTENCES = 6
# Rule 4.3: a multi-word noun of more than three words is banned outright.

# Fenced code, inline code, links, and tables are not prose and are not checked. A table cell
# is deliberately terse and would fail a sentence-length rule written for paragraphs.
FENCE = re.compile(r"^```")
TABLE_ROW = re.compile(r"^\s*\|")
HEADING = re.compile(r"^#{1,6}\s")
LIST_MARKER = re.compile(r"^\s*([-*+]|\d+\.)\s")


def prose_blocks(text: str) -> list[tuple[int, str]]:
    """Every paragraph of prose, with the line it starts on."""
    blocks: list[tuple[int, str]] = []
    current: list[str] = []
    start = 0
    in_fence = False
    for number, line in enumerate(text.split("\n"), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or TABLE_ROW.match(line) or HEADING.match(line):
            continue
        if not line.strip():
            if current:
                blocks.append((start, " ".join(current)))
                current = []
            continue
        # A list item is its own block. STE rule 6.5 counts sentences in a paragraph, and a
        # vertical list is the *remedy* the standard prescribes for complex text — counting its
        # items as one paragraph would report the fix as the fault.
        if LIST_MARKER.match(line):
            if current:
                blocks.append((start, " ".join(current)))
            current = [line.strip()]
            start = number
            continue
        if not current:
            start = number
        current.append(line.strip())
    if current:
        blocks.append((start, " ".join(current)))
    return blocks


def strip_markup(block: str) -> str:
    """Remove what is not prose: code spans, link targets, emphasis, list markers."""
    block = re.sub(r"`[^`]*`", "CODE", block)
    block = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", block)
    block = re.sub(r"[*_]{1,2}", "", block)
    return LIST_MARKER.sub("", block)


def sentences(block: str) -> list[str]:
    """Split on sentence-final punctuation, keeping abbreviations together."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", block)
    return [part.strip() for part in parts if part.strip()]


def word_count(sentence: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’/-]*", sentence))


# Rule 4.3 bans a multi-word noun of more than three words. It is not checked here: telling a
# noun cluster from an ordinary verb phrase needs a part-of-speech tagger, and the heuristic
# without one flagged "the other tools reason over screenshots". A check that cries wolf is
# worse than no check, because people learn to ignore it.


def check(path: Path) -> list[str]:
    problems: list[str] = []
    text = path.read_text()
    for start, raw in prose_blocks(text):
        block = strip_markup(raw)
        found = sentences(block)
        # Rule 6.5: one topic per paragraph, and no more than six sentences.
        if len(found) > MAX_PARAGRAPH_SENTENCES:
            problems.append(
                f"{path}:{start}: paragraph has {len(found)} sentences (limit {MAX_PARAGRAPH_SENTENCES})"
            )
        for sentence in found:
            count = word_count(sentence)
            if count > MAX_SENTENCE_WORDS:
                problems.append(
                    f"{path}:{start}: sentence has {count} words (limit {MAX_SENTENCE_WORDS}): "
                    f"{sentence[:70]}…"
                )
    return problems


def main(argv: list[str]) -> int:
    targets = [Path(argument) for argument in argv] or [
        Path("README.md"),
        *sorted(Path("documentation").glob("*.md")),
    ]
    problems: list[str] = []
    for path in targets:
        if path.is_file():
            problems.extend(check(path))
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} problems in {len(targets)} files")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
