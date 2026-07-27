"""The verification battery: the claims this rewrite makes, each checked by doing it.

There is no test suite in this repository — `pyproject.toml` points `testpaths` at a `tests/`
directory that contains no tests — so verification is built here rather than inherited. These
are not a substitute for one. They are the specific, falsifiable claims the architecture rests
on, written so that a person on a real machine can run them and see for themselves.

Two of the claims cannot be checked in a container and are the reason this exists as scripts
rather than as CI: the fork invariant is a macOS question (a multi-threaded parent aborts its
child inside the Objective-C runtime, with a message that names the wrong cause), and
confinement needs a kernel with Landlock or a working `sandbox-exec`.

Run the whole battery:

    uv run python -m scripts.verify

Or one stage:

    uv run python -m scripts.verify prototype

Each stage is independent, cleans up after itself, and answers with a verdict rather than a
traceback. Anything that needs a model provider says so and skips rather than failing, because
a missing API key is not a defect in the harness.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from scripts.verify.__main__ import main as run

    return run(argv)
