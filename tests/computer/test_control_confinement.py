"""The confined child must be able to start.

`control_screen` runs the model's script in a disposable child launched *by file path*, and that
child is confined by the session's own sandbox profile. The profile denies the user's home
wholesale and grants back only the interpreter roots — which cover frank when it is installed into
the environment, and do not cover it when it is imported from a source checkout or an editable
install. Screen control therefore worked when frank was installed and failed with "the sandbox
refused to run it" for everyone developing against the library.

These tests are about the child being able to start at all, which is why they use a trivial script
and a dispatch that answers nothing: the surfaces have their own tests, and what failed here was
upstream of any of them.
"""

from __future__ import annotations

import pytest

from frank.base import confinement
from frank.computer.control import run_control_script


async def _nothing(name, args, keywords):
    return {"ok": True, "called": name}


def test_the_package_is_readable_inside_a_confined_child():
    """The profile must name the directory frank is imported from.

    Asserted on the generated profile as well as by running a child, because the failure it guards
    is silent in the usual sense: the child dies before it can report anything, and the parent can
    only say that it produced no result."""
    profile_text = confinement.build_sbpl(confinement.Profile())
    assert confinement._package_root() in profile_text, (
        "the sandbox profile does not grant read access to frank's own package directory, so a "
        "child launched by file path cannot start when frank runs from a source checkout"
    )


@pytest.mark.asyncio
async def test_a_confined_child_runs_a_script():
    """The end-to-end version: a real profile, a real subprocess, a real result."""
    result = await run_control_script("result = 2 + 2", _nothing, profile=confinement.Profile())
    assert result.get("ok") is True, (
        f"a confined control child could not run: {result.get('error') or result}"
    )


@pytest.mark.asyncio
async def test_a_confined_child_can_call_a_primitive():
    """A primitive call has to cross the pipes in both directions, which is the whole mechanism."""
    async def one_element(name, args, keywords):
        return [{"id": "e1", "name": "Save", "role": "AXButton"}]

    result = await run_control_script(
        "result = find_many('save')", one_element, profile=confinement.Profile())
    assert result.get("ok") is True, result.get("error") or result


@pytest.mark.asyncio
async def test_an_unconfined_child_still_runs():
    """`profile=None` is the embedding case that never went through the sandbox at all."""
    result = await run_control_script("result = 1", _nothing, profile=None)
    assert result.get("ok") is True, result.get("error") or result
