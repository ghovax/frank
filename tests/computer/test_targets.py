"""A target is a place, and its identity comes from the platform rather than from a display name.

The bug that motivated this module: `app="RStudio"` with two RStudio windows open resolved to the
wrong process, and nothing in the addressing scheme could express which one was meant. These tests
are about the properties that make that impossible — every target has an id, ids are unique, and
an id that no longer exists resolves to nothing rather than to something else.
"""

from __future__ import annotations

from frank.computer import targets


def test_every_target_has_an_identity_and_they_are_unique():
    """Two windows of one application must be distinguishable, which a display name cannot do."""
    found = targets.list_targets()
    if not found:
        return  # a headless machine has no windows; nothing to assert about
    identifiers = [target.id for target in found]
    assert all(identifiers), "a target without an id cannot be addressed"
    assert len(identifiers) == len(set(identifiers)), (
        f"two targets share an id, so one of them cannot be reached: {identifiers}"
    )


def test_an_id_says_which_kind_of_place_it_is():
    for target in targets.list_targets():
        assert target.id.startswith((f"{targets.NATIVE_PREFIX}-", f"{targets.BROWSER_PREFIX}-")), target.id
        assert target.surface in {"computer", "browser"}, target.surface


def test_the_surface_is_not_shown_to_the_model():
    """`surface` routes the dispatcher; it is the implementation detail this design hides."""
    for target in targets.list_targets():
        assert "surface" not in target.described(), (
            "the described form leaks which machinery answers, which is what the model must not "
            "have to reason about"
        )


def test_a_target_that_does_not_exist_resolves_to_nothing():
    """Never to something else — the failure the display-name scheme produced."""
    assert targets.find_target("win-999999999") is None
    assert targets.find_target("") is None
    assert targets.find_target("nonsense") is None


def test_a_listing_can_be_diffed():
    """The listing is sent as a change against a baseline, so the diff has to be well behaved."""
    before = targets.list_targets()
    assert targets.difference(before, before) == {}, "an unchanged world must report no changes"

    if before:
        without_first = before[1:]
        removed = targets.difference(before, without_first)
        assert removed.get("removed") == [before[0].id]
        added = targets.difference(without_first, before)
        assert [entry["id"] for entry in added.get("added", [])] == [before[0].id]


def test_furniture_is_filtered_out():
    """A menu-bar extra is on screen and is not a place anybody means."""
    for target in targets.list_targets():
        assert target.app not in targets.FURNITURE_OWNERS, target.app
