"""Obsidian vault integration: create and read paper notes backed by Zotero."""
import pathlib

import frontmatter
import mistletoe
from mistletoe.block_token import Document, Heading, Paragraph
from mistletoe.markdown_renderer import MarkdownRenderer
from mistletoe.span_token import RawText


class _SpacedMarkdownRenderer(MarkdownRenderer):
    """MarkdownRenderer that inserts a blank line between every top-level block."""

    def blocks_to_lines(self, tokens, max_line_length):
        first = True
        for token in tokens:
            if not first:
                yield ""
            first = False
            yield from self.render_map[token.__class__.__name__](
                token, max_line_length=max_line_length
            )

from .zotero import zotero_get

# Vault is at the repo root, five levels above this file:
# obsidian.py → scholar/ → scripts/ → literature-search/ → skills/ → .claude/ → repo root
_DEFAULT_VAULT = pathlib.Path(__file__).parents[5] / "vault"


def _make_heading(level: int, text: str) -> Heading:
    heading = Heading.__new__(Heading)
    heading.level = level
    heading.closing_sequence = ""
    heading.children = [RawText(text)]
    return heading


def _make_paragraph(text: str) -> Paragraph:
    # Paragraph.__new__ requires a list argument to distinguish itself from setext headings.
    paragraph = Paragraph.__new__(Paragraph, [""])
    paragraph.children = [RawText(text)]
    return paragraph


def _build_body(title: str, abstract: str | None) -> str:
    """Build the note body by constructing a mistletoe AST and rendering it to Markdown."""
    with _SpacedMarkdownRenderer() as renderer:
        block_nodes = [_make_heading(1, title)]
        if abstract:
            block_nodes.append(_make_paragraph(abstract.strip()))
        block_nodes.append(_make_heading(2, "Comments"))
        document = Document.__new__(Document)
        document.footnotes = {}
        document.children = block_nodes
        return renderer.render(document)


def obsidian_create(zotero_key: str) -> dict:
    """Create an Obsidian note for a Zotero attachment key if it does not already exist.

    Writes vault/{zotero_key}.md with YAML frontmatter (doi, zotero_key), an H1
    title, the abstract, and an empty Comments section for the user's own notes.
    Idempotent: if the file already exists it is left untouched and {"created": False}
    is returned. Uses the Zotero Web API — no local database access.
    """
    note_path = _DEFAULT_VAULT / f"{zotero_key}.md"
    if note_path.exists():
        return {"created": False, "path": str(note_path)}

    zotero_result = zotero_get(zotero_key, children=True)
    attachment_data = zotero_result["result"]["data"]

    parent_key = attachment_data.get("parentItem")
    title: str = attachment_data.get("title", zotero_key)
    doi: str | None = None
    abstract: str | None = None
    authors: list[str] = []
    date: str | None = None
    publisher: str | None = None
    if parent_key:
        parent_data = zotero_get(parent_key)["result"]["data"]
        title = parent_data.get("title") or title
        doi = parent_data.get("DOI") or None
        abstract = parent_data.get("abstractNote") or None
        date = parent_data.get("date") or None
        publisher = parent_data.get("publisher") or None
        authors = [
            " ".join(filter(None, [c.get("firstName"), c.get("lastName")]))
            for c in parent_data.get("creators", [])
            if c.get("creatorType") == "author"
        ]

    body = _build_body(title, abstract)
    metadata: dict = {"zotero_key": zotero_key}
    if doi:
        metadata["doi"] = doi
    if authors:
        metadata["authors"] = authors
    if date:
        metadata["date"] = date
    if publisher:
        metadata["publisher"] = publisher
    post = frontmatter.Post(body, **metadata)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(frontmatter.dumps(post) + "\n\n", encoding="utf-8")

    return {"created": True, "path": str(note_path), "key": zotero_key,
            "title": title, "doi": doi}


def obsidian_read(zotero_key: str) -> dict:
    """Read an Obsidian note and return its path and content.

    Returns {"path", "content"} — the raw file text so the caller can read the
    user's own comments. For annotations use zotero_get(key, children=True).
    """
    note_path = _DEFAULT_VAULT / f"{zotero_key}.md"
    content: str | None = note_path.read_text(encoding="utf-8") if note_path.exists() else None
    return {"path": str(note_path), "content": content}
