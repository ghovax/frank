"""What a call asks for beyond its box, and whether that needs a decision.

What decides here is what the harness can check for itself. The operating system confines every
child to the paths and the network the session was given, so there is one question to ask, and
it is a question about *reach*: does this call ask for something outside that box? If it does
not, it runs — whatever it does in there, it does inside a boundary somebody chose. If it does,
somebody decides: the person, or the reviewer.

Nothing here reads the model's account of its own call. A label a call attaches to itself is a
claim rather than a fact, and a scan of a command's text for verbs that might mean writing is a
question the shell answers and a word list cannot. :func:`verdict_for` is a pure function of
four named things, none of which the model supplies — there is no parameter through which such
a claim could arrive, so no later edit can quietly make one load-bearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from frank.base.confinement import AccessRequest, Profile, _contained_in, expand
from frank.protocol.events import PermissionReason


@dataclass(frozen=True)
class Escape:
    """What one call asks for beyond the confinement it already has.

    Empty is the ordinary case and the interesting one: an empty escape is a call that stays
    inside its box, and a call that stays inside its box raises nothing.
    """

    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    network: bool = False

    def __bool__(self) -> bool:
        return bool(self.reads or self.writes or self.network)

    def summary(self, explanation: str = "") -> str:
        """What the person deciding is shown: the reach asked for, and the reason given for it."""
        wanted = []
        if self.writes:
            wanted.append(f"write {', '.join(self.writes)}")
        if self.reads:
            wanted.append(f"read {', '.join(self.reads)}")
        if self.network:
            wanted.append("reach the network")
        asked = "; ".join(wanted) or "reach beyond its confinement"
        # The reason is the model's own, and it is the only thing that makes the path meaningful.
        return f"Needs to {asked} — {explanation}" if explanation else f"Needs to {asked}"


def escape_of(
    request: Optional[AccessRequest],
    profile: Optional[Profile],
    *,
    workspace: str = "",
) -> Escape:
    """What ``request`` asks for that ``profile`` does not already permit.

    Reads the call's `access_request` and nothing else. Not its command text, not its
    explanation, and not any label it attached to itself — a call's reach is a property of what
    it asked for, and the box is what answers for everything it did not ask for.

    Already-held access is subtracted here rather than at the gate, so a session that was
    granted `/opt/out` earlier does not raise a second gate for `/opt/out/thing`: containment,
    not equality, exactly as the profile itself matches.
    """
    if request is None or not request.wants_widening or profile is None:
        return Escape()
    readable = tuple(profile.filesystem.readable) + tuple(profile.filesystem.writable)
    held_reads = set(_contained_in(request.reads, readable, workspace=workspace))
    held_writes = set(_contained_in(request.writes, tuple(profile.filesystem.writable), workspace=workspace))
    return Escape(
        reads=tuple(path for path in request.reads if path not in held_reads),
        writes=tuple(path for path in request.writes if path not in held_writes),
        network=bool(request.network) and not profile.network,
    )


@dataclass(frozen=True)
class Verdict:
    """What to do with one call: run it, put it to somebody, or refuse it outright."""

    kind: Literal["run", "ask", "refuse"]
    reason: Optional[PermissionReason] = None
    message: str = ""

    @property
    def runs(self) -> bool:
        return self.kind == "run"


#: Where a rule's decision may land.
RULE_ALLOW = "allow"
RULE_ASK = "ask"
RULE_DENY = "deny"


def verdict_for(
    *,
    escape: Escape,
    rule: str,
    profile: Optional[Profile],
    workspace: str = "",
) -> Verdict:
    """The decision on one call. Pure, total, and blind to anything the model said about itself.

    Four inputs, in the order they settle the question:

    1. **The rule** the person wrote for this command, tool or server. ``deny`` is final and is
       not a question — a standing refusal must not be reachable by asking nicely.
    2. **The deny list**, for the paths an escape names. Same argument, one level down: a path
       somebody declared off-limits is not widenable by any approval.
    3. **The escape.** Nothing asked for means nothing to decide, and the call runs inside its
       box. This is the branch that fires for almost every command, and it is why the harness
       stopped interrupting people about `ls`.
    4. **What the profile permits without asking** — the ``grantable`` list, which is how a
       person says in advance "these paths, yes, don't wake me".

    ``rule == "ask"`` still forces a gate even with nothing to escape, because that is what a
    person means by writing it: this command, always, in front of me.
    """
    if rule == RULE_DENY:
        return Verdict(
            kind="refuse",
            reason=PermissionReason(kind="denied_by_rules"),
            message="Your permission rules deny this.",
        )
    if profile is not None and escape:
        refused = _refused_by_deny_list(escape, profile, workspace=workspace)
        if refused:
            return Verdict(
                kind="refuse",
                reason=PermissionReason(kind="denied_path", paths=list(refused)),
                message=(
                    "These paths are on the deny list, which no approval can widen: "
                    + ", ".join(refused)
                ),
            )
    if not escape:
        return Verdict(kind="ask") if rule == RULE_ASK else Verdict(kind="run")
    if rule != RULE_ASK and _pre_cleared(escape, profile, workspace=workspace):
        return Verdict(kind="run")
    return Verdict(
        kind="ask",
        reason=PermissionReason(
            kind="reaches_outside_confinement",
            paths=list(escape.reads + escape.writes),
        ),
    )


def _refused_by_deny_list(escape: Escape, profile: Profile, *, workspace: str) -> list[str]:
    """The paths in ``escape`` that lie under something the profile denies."""
    from pathlib import Path

    denied = [
        Path(resolved) for entry in profile.filesystem.deny
        if (resolved := expand(entry, workspace=workspace))
    ]
    if not denied:
        return []
    refused = []
    for entry in escape.reads + escape.writes:
        resolved = expand(entry, workspace=workspace)
        if not resolved:
            continue
        candidate = Path(resolved)
        if any(candidate == root or candidate.is_relative_to(root) for root in denied):
            refused.append(entry)
    return sorted(dict.fromkeys(refused))


def _pre_cleared(escape: Escape, profile: Optional[Profile], *, workspace: str) -> bool:
    """Whether every path this escape names sits inside what a person pre-approved.

    All-or-nothing, deliberately: a request that reaches one listed path and one unlisted one is
    asked about whole. Approving the half that was pre-cleared and gating the rest would split
    one intent into two decisions and show the person a request that no longer matches what the
    agent asked for.

    The network is never pre-cleared by a path list. Somebody who wrote a directory under
    ``grantable`` said something about that directory, and reading it as also opening the wire
    would be answering a question they were not asked.
    """
    if profile is None or escape.network:
        return False
    return profile.grants_without_asking(escape.reads + escape.writes, workspace=workspace)
