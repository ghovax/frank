"""What a call asks for beyond its box, and whether that needs a decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from langmesh.base.confinement import AccessRequest, Profile, _contained_in, expand
from langmesh.protocol.events import PermissionReason


@dataclass(frozen=True)
class Escape:
    """What one call asks for beyond its confinement. Empty means it stays inside, and raises nothing."""

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
        # The model's own reason, without which the path has no purpose attached to it.
        return f"Needs to {asked} — {explanation}" if explanation else f"Needs to {asked}"


def escape_of(
    request: Optional[AccessRequest],
    profile: Optional[Profile],
    *,
    workspace: str = "",
) -> Escape:
    """What ``request`` asks for that ``profile`` does not already permit, by containment."""
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


#: Where a rule's decision may land, in the words a person writes in their configuration.
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
    """The decision on one call, from the rule, the deny list, the escape, and what is pre-cleared."""
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
    """Whether every path this escape names was pre-approved. All-or-nothing, and never the network."""
    if profile is None or escape.network:
        return False
    return profile.grants_without_asking(escape.reads + escape.writes, workspace=workspace)
