"""The session worker's entry point, reached by `exec` from the prototype.

The prototype forks and immediately execs this, rather than running the session in the forked
image. That is the whole difference between a session that works and one that dies of
`SIGSEGV` inside `getaddrinfo` on its first model call: a forked child inherits the parent's
copy of Network.framework and the Objective-C runtime, and neither is usable after a fork
without an exec.

Two descriptors survive the exec, named on the command line because a number is all that can
be passed across it:

* the **assignment pipe**, which the prototype fills before forking. The assignment carries
  this session's capability token and the daemon's, so it travels on a pipe rather than in
  ``argv``, which any other process on the machine can read out of ``ps``.
* the **ready pipe**, which this process writes once it is listening, and which the prototype
  is watching to know whether the session came up.

Both are passed as raw file-descriptor numbers, which is only meaningful because
:func:`os.set_inheritable` was called on them before the exec — the default is for a
descriptor to be closed by it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys

from frank.base.fork_protocol import StartFailure

logger = logging.getLogger(__name__)

# Why a session refused to start, as a value rather than a sentence. Prose belongs in the log,
# where a person reads it; what crosses the ready pipe is consumed by the prototype and ends up
# in a session record, so it wants to be matchable and stable.
# The vocabulary lives in `base.fork_protocol`, because the reader of this value is a
# different process and a code only works if both ends agree on it in one place.


def _configure_logging() -> None:
    """The same file the daemon and the prototype write.

    A session is its own process now, so it configures its own logging — nothing is inherited
    across an `exec`. Without this a turn that fails logs `Agent turn failed` to a stderr that
    nothing keeps, which is exactly how an `AttributeError` on every single turn stayed
    invisible long enough to be mistaken for a network problem."""
    from frank.base.paths import log_file_path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file_path("frankd"))],
    )


def _read_assignment(descriptor: int) -> dict:
    """Drain the assignment pipe. Reads to EOF rather than once, because a pipe is a stream and
    a short read would produce a truncated assignment that parses as nothing useful."""
    chunks: list[bytes] = []
    with os.fdopen(descriptor, "rb", closefd=True) as pipe:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
    payload = json.loads(b"".join(chunks).decode() or "{}")
    if not isinstance(payload, dict):
        raise ValueError(f"assignment must be an object, got {type(payload).__name__}")
    return payload


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        print(
            "usage: frank session <assignment-fd> <ready-fd> "
            "(internal; the prototype execs this)",
            file=sys.stderr,
        )
        return 2

    try:
        assignment_fd, ready_fd = (int(value) for value in arguments)
    except ValueError:
        print(f"session: file descriptors must be integers, got {arguments}", file=sys.stderr)
        return 2

    _configure_logging()

    # Import before reading the assignment, not after. The order is the whole reason a session
    # can start quickly: this is around two and a half seconds — most of it litellm — and none
    # of it depends on which session this will become. Paying it first means the prototype can
    # start a child before anyone has asked for one, let it do this work while nobody is
    # waiting, and leave it parked on the read below. Handing that child a session is then a
    # write down a pipe. Read-then-import made every session pay the cost in full, with the
    # caller waiting for all of it.
    #
    # `serve` alone is not enough and is the easy mistake: it is a third of a second, because it
    # deliberately defers the expensive graph into the body of `serve()`. The two modules named
    # here are where litellm, the model clients and the tool registry actually come from, so
    # they are imported by name rather than by whatever happens to be reachable from `run`.
    from frank.worker.serve import run
    import frank.worker.server  # noqa: F401 — imported for its cost, not its name
    import frank.worker.session  # noqa: F401 — see above

    # The embedding model that ranks screen elements, loaded here for the same reason and by
    # the same logic: it takes about three quarters of a second, it is identical for every
    # session, and a parked worker has nothing else to do. Left lazy, the first `control_screen`
    # search of a session paid it while someone waited.
    with contextlib.suppress(Exception):
        from frank.computer.retrieval import _dense_model

        _dense_model()

    try:
        assignment = _read_assignment(assignment_fd)
    except Exception:  # noqa: BLE001 — a session that cannot read its assignment cannot start
        logger.exception("Session could not read its assignment")
        # Said on the ready pipe as well as in the log, because the prototype is waiting on that
        # pipe and would otherwise report a session that simply never arrived.
        with os.fdopen(ready_fd, "wb", closefd=True) as ready:
            ready.write(json.dumps({"ready": False, "reason": StartFailure.ASSIGNMENT_UNREADABLE}).encode())
        return 1

    return run(assignment, ready_fd)
