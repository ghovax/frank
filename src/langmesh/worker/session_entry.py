"""The session worker's entry point, reached by `exec` from the prototype."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys

from langmesh.base.fork_protocol import StartFailure

logger = logging.getLogger(__name__)

# Why a session refused to start, as a value rather than a sentence, since the prototype is what reads it.


def _configure_logging() -> None:
    """The same log file the daemon and the prototype write, configured here because nothing survives an `exec`."""
    from langmesh.base.paths import log_file_path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file_path("langmeshd"))],
    )


class PrototypeGone(Exception):
    """The prototype closed the assignment pipe without writing one, which means it died while this worker was parked."""


def _read_assignment(descriptor: int) -> dict:
    """Drain the assignment pipe to end-of-file, since a short read would produce a truncated assignment."""
    chunks: list[bytes] = []
    with os.fdopen(descriptor, "rb", closefd=True) as pipe:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode()
    # End-of-file with nothing before it is the prototype dying rather than an empty assignment.
    if not raw:
        raise PrototypeGone
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"assignment must be an object, got {type(payload).__name__}")
    return payload


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        print(
            "usage: langmesh session <assignment-fd> <ready-fd> <lifeline-fd> "
            "(internal; the prototype execs this)",
            file=sys.stderr,
        )
        return 2

    try:
        assignment_fd, ready_fd, lifeline_fd = (int(value) for value in arguments)
    except ValueError:
        print(f"session: file descriptors must be integers, got {arguments}", file=sys.stderr)
        return 2

    _configure_logging()

    # Import before reading the assignment, which is the whole reason a session can start quickly.
    from langmesh.worker.serve import run
    import langmesh.worker.server  # noqa: F401 — imported for its cost, not its name
    import langmesh.worker.session  # noqa: F401 — see above

    # The embedding model that ranks screen elements, loaded here for the same reason and by the same logic.
    with contextlib.suppress(Exception):
        from langmesh.computer.retrieval import _dense_model

        _dense_model()

    try:
        assignment = _read_assignment(assignment_fd)
    except PrototypeGone:
        # No ready report and no traceback, since the only thing that reads that pipe is what has just gone.
        logger.info("the prototype went away before this worker was given a session; stopping")
        return 0
    except Exception:  # noqa: BLE001 — a session that cannot read its assignment cannot start
        logger.exception("session could not read its assignment")
        # Said on the ready pipe as well as in the log, because the prototype is waiting on that pipe.
        with os.fdopen(ready_fd, "wb", closefd=True) as ready:
            ready.write(json.dumps({"ready": False, "reason": StartFailure.ASSIGNMENT_UNREADABLE}).encode())
        return 1

    return run(assignment, ready_fd, lifeline_fd)
