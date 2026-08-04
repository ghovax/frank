"""Guards on the native key, which had none until a regression shipped through the gap.

The browser tests in `test_encoding_strategies` load only browser corpora, deliberately — pooling
the surfaces would score compositions on windows that cannot supply a URL or a tooltip. The
consequence was that the native key had no coverage at all, and a change that keyed 628 elements
by the name of their kind reached the repository without a single test noticing.

What that regression exposed is that top-1 accuracy is structurally blind to it. An element that
cannot be retrieved contributes no query, so burying one *improves* the average over the queries
that remain. Every test here is therefore about reachability first and ranking second.
"""

from __future__ import annotations

import pytest

from tests.retrieval.corpus import load_all_corpora


@pytest.fixture(scope="module")
def corpora():
    loaded = load_all_corpora(surface_name="native")
    if not loaded:
        pytest.skip("no native corpora; run `uv run python -m tests.retrieval.harvest_native`")
    return loaded


def native_key(element) -> str:
    """The key `engine.documents` builds: what it is called, then what it says, then its kind."""
    return element.name.strip() or element.value.strip() or element.role_description.strip()


def test_every_element_with_words_has_a_key(corpora):
    """An empty key is an absence, not a bad ranking — no query reaches it at any depth."""
    unreachable = [
        (corpus.site_name, element.role)
        for corpus in corpora for element in corpus.elements
        if (element.name.strip() or element.value.strip() or element.role_description.strip())
        and not native_key(element)
    ]
    assert not unreachable, f"{len(unreachable)} elements carry words but key to nothing"


def test_content_is_not_shadowed_by_the_name_of_its_kind(corpora):
    """The regression this file exists for.

    `AXRoleDescription` is present on every element, so placing it ahead of the value fallback
    silenced that fallback entirely: forty-seven Finder rows keyed "text" while Recents, Desktop
    and Documents sat unread. An element that displays words must key on those words."""
    shadowed = []
    for corpus in corpora:
        for element in corpus.elements:
            if not element.value.strip() or element.name.strip():
                continue
            if native_key(element) != element.value.strip():
                shadowed.append((corpus.site_name, element.role_description, element.value[:30]))
    assert not shadowed, (
        f"{len(shadowed)} elements key on the name of their kind while their own content goes "
        f"unread, e.g. {shadowed[:3]}"
    )


def test_few_elements_share_a_key_with_many_others(corpora):
    """A key shared by dozens of elements cannot single any of them out.

    Not a ranking assertion — a degeneracy one. The regression produced 628 elements sharing a
    handful of keys, which no accuracy average over answerable queries could see."""
    for corpus in corpora:
        counts: dict[str, int] = {}
        for element in corpus.elements:
            key = native_key(element)
            if key:
                counts[key] = counts.get(key, 0) + 1
        worst_key, worst_count = max(counts.items(), key=lambda item: item[1], default=("", 0))
        assert worst_count <= max(10, len(corpus.elements) // 8), (
            f"{corpus.site_name}: {worst_count} elements share the key {worst_key!r}, so a query "
            f"cannot tell them apart"
        )


def test_the_fixtures_record_attributes_rather_than_assembled_strings(corpora):
    """A fixture stores what a surface published; anything built from it belongs to analysis.

    The native fixtures once held a `source` column of strings assembled at harvest time out of
    `role`, `name` and `value`. It carried no information the other columns lacked, could only be
    read back in the shape it was written, and quietly guaranteed the result of any experiment run
    against it."""
    for corpus in corpora:
        for element in corpus.elements:
            assert not element.source, (
                "native fixtures carry a pre-assembled `source`; record published attributes and "
                "assemble the declaration in analysis instead"
            )
