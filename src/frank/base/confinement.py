"""What a tool's child process is actually allowed to do, enforced by the operating system.

The harness spawns two kinds of child that run code nobody wrote in advance: the shell command a
`bash` call asks for, and the Python a `control_screen` script is made of. This module is the
boundary both of them run inside. A heuristic over source is not confinement, and bounding
runaway resource use is not bounding authority, so neither stands in for what is here.

Almost all of a confined child is POSIX and needs no platform code: resource limits are
``setrlimit(2)`` under their own constant names, the file-creation mask is ``umask(2)``, priority
is ``nice(2)``, and the environment is built rather than inherited. All four are applied between
fork and exec, which is where a Unix process has always configured itself.

Two things have no POSIX spelling — which files a process may touch, and whether it may reach the
network — and they are the two that matter. macOS answers with `sandbox-exec` and a generated SBPL
profile; Linux answers with Landlock for the filesystem and a network namespace for the network.
The macOS interface is deprecated by Apple and depended on anyway, because the alternatives either
need privileges the harness does not have or take away the user's own files, which is the thing the
harness exists to reach.

A profile is resolved once, when a session is created, and clamped against the session that created
it: path sets intersect, the stricter network setting wins, the lower limit wins. Without that a
confined session could create an unconfined peer and the boundary would be one call deep.
"""

from __future__ import annotations

import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Optional

# How a missing backend is handled. `required` refuses to create a session at all, which is the
# default and the direct answer to how this module came to exist: a setting that claims to confine
# and does not is worse than one that refuses. `preferred` runs with the POSIX half only and says
# so; `off` does not confine.
ENFORCE_REQUIRED = "required"
ENFORCE_PREFERRED = "preferred"
ENFORCE_OFF = "off"

# The rlimits the configuration may name. Restricted to the ones that mean something for a tool
# child, so a typo is an error rather than a silently ignored key.
_SUPPORTED_LIMITS = ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_CORE", "RLIMIT_NOFILE")

# Environment variables a child needs to be a usable Unix process. Everything else the worker holds
# — API keys above all — is left behind unless a profile passes it explicitly.
_BASE_ENVIRONMENT_KEYS = (
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL", "TERM", "TZ", "USER",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
)


class ConfinementUnavailable(RuntimeError):
    """No backend can enforce a profile on this machine, and the policy says that is fatal."""


@dataclass(frozen=True)
class Filesystem:
    """Which paths a child may read and which it may write.

    The system is readable and is not listed: `/usr` and `/etc` are not secrets, and denying them
    breaks every command while protecting nothing. What the lists govern is the user's own home,
    which is denied by default — so `readable` is the allowlist that keeps toolchains working and
    `deny` is what wins over it."""

    readable: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    # Paths a runtime grant may open without asking a person. Not an allowance in itself: a path
    # listed here is reachable only once the agent has actually requested it, which is what keeps
    # it different from `writable`. Nothing is listed by default, so every request is asked about
    # until somebody decides otherwise.
    grantable: tuple[str, ...] = ()
    # Exact files the person handed to this session, and the one thing that outranks `deny`.
    #
    # `deny` stops the agent from *going looking* through a directory. It was never meant to stop
    # somebody from passing it one named file, and treating those as the same thing is what made
    # an attachment from `~/Downloads` unreadable — which is where almost every attachment comes
    # from. The model cannot put anything in this list: there is no tool that attaches a file, so
    # the only way a path arrives here is that a person chose it in the interface.
    #
    # Exact files only, never a directory. That is the whole of why this is safe: an allowance
    # for `~/Downloads/report.pdf` opens that file and tells the agent nothing about what else
    # is in the folder.
    attached: tuple[str, ...] = ()

    def intersect(self, parent: "Filesystem") -> "Filesystem":
        """This filesystem clamped against a parent's: never wider, and the parent's denials are
        inherited whole. Path containment rather than string equality, so a child asking for a
        subdirectory of what its parent holds keeps it."""
        return Filesystem(
            readable=_contained_in(self.readable, parent.readable),
            writable=_contained_in(self.writable, parent.writable),
            deny=tuple(dict.fromkeys(self.deny + parent.deny)),
            # What may be granted without asking narrows exactly as an allowance does: a child
            # cannot be handed a quieter approval path than its parent holds.
            grantable=_contained_in(self.grantable, parent.grantable),
            # Deliberately dropped. A file the person attached to one conversation was handed
            # to *that* session, and a peer it creates is doing different work for a different
            # reason. Carrying the allowance down would turn one deliberate act into a standing
            # exemption across a subtree the person never saw.
            attached=(),
        )


@dataclass(frozen=True)
class AccessRequest:
    """What one call says it needs beyond the confinement it already has.

    ``mutates`` is
    three-valued on purpose. ``False`` says the call changes nothing; ``True`` says it does;
    ``None`` says the model made no claim, because it omitted the request entirely. The third
    case has to stay distinguishable from the second — treating "said nothing" as "said it
    mutates" would be safe but would lose the signal that nothing was declared, which is what
    the static scan needs to know before it decides whether `unknown` should escalate.
    """

    mutates: Optional[bool] = None
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    network: bool = False

    @property
    def wants_widening(self) -> bool:
        """Whether this asks for anything at all. A request that only declares ``mutates`` is a
        statement about the call, not a request for access, and must not raise a gate."""
        return bool(self.reads or self.writes or self.network)


def parse_access_request(value: object) -> tuple[Optional[AccessRequest], str]:
    """One tool argument as an :class:`AccessRequest`, or a sentence saying why it is not.

    Returns ``(None, "")`` for an absent request, which is the ordinary case and not an error.
    Validation is strict about the shape and forgiving about nothing: a malformed request is
    refused rather than partially understood, because the half that parsed would become a grant
    request nobody wrote.
    """
    if value is None:
        return None, ""
    if not isinstance(value, dict):
        return None, "access_request must be an object."
    unknown = sorted(set(value) - {"mutates", "reads", "writes", "network"})
    if unknown:
        return None, f"access_request does not accept: {', '.join(unknown)}."
    if "mutates" not in value:
        return None, "access_request must state `mutates` when it is present."
    if not isinstance(value.get("mutates"), bool):
        return None, "access_request.mutates must be a boolean."
    network = value.get("network", False)
    if not isinstance(network, bool):
        return None, "access_request.network must be a boolean."

    paths: dict[str, tuple[str, ...]] = {}
    for name in ("reads", "writes"):
        entries = value.get(name) or []
        if isinstance(entries, str):
            entries = [entries]
        if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
            return None, f"access_request.{name} must be a list of paths."
        cleaned = tuple(entry.strip() for entry in entries if entry.strip())
        # A request for the whole filesystem is not a request; it is a way of not being asked
        # again. Refused at the surface rather than left for the classifier, so that the answer
        # is the same however the session is configured to decide.
        for entry in cleaned:
            if entry in ("/", "~", "/*", "~/", "~/*"):
                return None, (
                    f"access_request.{name} may not name the whole filesystem ({entry}). "
                    "Ask for the narrowest path that does the work."
                )
        paths[name] = cleaned

    return AccessRequest(
        mutates=bool(value["mutates"]),
        reads=paths["reads"], writes=paths["writes"], network=network,
    ), ""


@dataclass(frozen=True)
class Grant:
    """One widening that was approved, what it was approved for, and who approved it.

    Held by the session rather than by the call, because a grant re-asked on every command in a
    build produces a row of gates nobody reads by the fourth. How often somebody is asked is
    itself a security property, and it does not point the way intuition says: an approval asked
    too often trains the person to approve without looking.

    ``purpose`` is the explanation the call carried when the grant was approved. It is kept so
    the person can see, in the listing, what they said yes to — a path with no reason beside it
    is a permission nobody can audit later.

    ``approved_by`` names the authority behind the widening — a person at the keyboard, a rule
    they wrote in advance, or the reviewer a session runs under when nobody is there. A grant is
    the only thing that widens a confinement, so every one of them says who said yes.

    ``whole_disk`` is the answer to a command the operating system refused. It cannot name the
    path it wanted — a Seatbelt denial is an ``EPERM`` with no file in it — so the only honest
    thing to offer is "let this one command reach past the workspace". It widens to the root and
    **does not** turn confinement off: the deny list is applied after every allowance on both
    backends, so what a person declared off-limits stays off-limits through a grant that names
    everything. There is deliberately no way to spell "run this unconfined"."""

    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    network: bool = False
    whole_disk: bool = False
    purpose: str = ""
    granted_at: str = ""
    approved_by: str = ""

    def as_dict(self) -> dict:
        return {
            "reads": list(self.reads), "writes": list(self.writes),
            "network": self.network, "whole_disk": self.whole_disk,
            "purpose": self.purpose, "granted_at": self.granted_at,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Grant":
        data = data or {}
        return cls(
            reads=tuple(data.get("reads") or ()),
            writes=tuple(data.get("writes") or ()),
            network=bool(data.get("network", False)),
            whole_disk=bool(data.get("whole_disk", False)),
            purpose=str(data.get("purpose") or ""),
            granted_at=str(data.get("granted_at") or ""),
            approved_by=str(data.get("approved_by") or ""),
        )


#: Who approved a grant. Not an enum, because it crosses the wire and a session record written
#: by one release is read by the next; the three spellings are the whole of it.
APPROVED_BY_PERSON = "person"
APPROVED_BY_RULE = "rule"
APPROVED_BY_REVIEWER = "reviewer"


def approved(
    request: "AccessRequest | None" = None,
    *,
    by: str,
    purpose: str = "",
    whole_disk: bool = False,
) -> Grant:
    """Mint a grant. The one place a :class:`Grant` is built outside deserialization.

    Every widening in the harness comes through here, so "who said yes" is asked once, and a
    site that cannot answer it cannot widen anything.
    """
    if by not in (APPROVED_BY_PERSON, APPROVED_BY_RULE, APPROVED_BY_REVIEWER):
        raise ValueError(f"a grant must name its authority, not {by!r}")
    return Grant(
        reads=tuple(request.reads) if request is not None else (),
        writes=tuple(request.writes) if request is not None else (),
        network=bool(request.network) if request is not None else False,
        whole_disk=whole_disk,
        purpose=purpose,
        granted_at=_now(),
        approved_by=by,
    )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Profile:
    """One child's confinement, whole. Frozen because it is resolved once and then only narrowed."""

    filesystem: Filesystem = field(default_factory=Filesystem)
    # Closed, like the filesystem it sits beside. A box with a hole in it is not a box: a command
    # that can reach the network can post the contents of the workspace anywhere, and every
    # argument for confining the disk applies unchanged to the wire. It opens the way the disk
    # opens — a call asks for it, and somebody or something says yes.
    network: bool = False
    limits: dict[str, int] = field(default_factory=dict)
    umask: Optional[int] = None
    nice: int = 0
    environment: dict[str, str] = field(default_factory=dict)
    enforce: str = ENFORCE_REQUIRED

    def clamp(self, parent: Optional["Profile"]) -> "Profile":
        """This profile as a child of ``parent`` — never wider on any axis.

        The clamp is what makes confinement compose. Path sets intersect, the stricter network
        setting wins, the lower limit wins, and the stricter enforcement wins, so a session cannot
        hand a peer authority it does not hold itself."""
        if parent is None:
            return self
        limits = dict(parent.limits)
        for name, value in self.limits.items():
            limits[name] = min(value, limits[name]) if name in limits else value
        strictness = (ENFORCE_OFF, ENFORCE_PREFERRED, ENFORCE_REQUIRED)
        return Profile(
            filesystem=self.filesystem.intersect(parent.filesystem),
            network=self.network and parent.network,
            limits=limits,
            umask=self.umask if parent.umask is None else (self.umask if self.umask is not None and self.umask > parent.umask else parent.umask),
            nice=max(self.nice, parent.nice),
            environment={**self.environment},
            enforce=max(self.enforce, parent.enforce, key=strictness.index),
        )

    def narrowed(self, *, writable: Iterable[str], network: bool, workspace: str = "") -> "Profile":
        """A stricter variant of this profile, for a child that needs less than the session does.

        The `control_screen` child is the case: everything it can do is bridged to its parent over
        a pipe, so it needs no network and no filesystem beyond somewhere to put a temporary file.
        Derived rather than separately configurable — two profiles to configure would be two
        profiles to get wrong, and there is no case for giving that child a network."""
        return replace(
            self,
            filesystem=Filesystem(
                readable=self.filesystem.readable,
                # A strict intersection, with no fallback. An `or tuple(writable)` here would
                # have meant that a session permitted to write nowhere useful handed its child
                # a directory it did not itself hold — a widening, in the one method whose
                # whole purpose is to narrow.
                writable=_contained_in(tuple(writable), self.filesystem.writable, workspace=workspace),
                deny=self.filesystem.deny,
                # Explicitly nothing. A narrowed child is a helper the harness spawns for one
                # bridged purpose, not a session anybody negotiates with, so there is no route
                # by which it could ask for more and nothing to gain by leaving one open.
                grantable=(),
            ),
            network=self.network and network,
        )

    def with_grant(self, grant: Grant, *, workspace: str = "") -> "Profile":
        """This profile plus one approved widening. The mirror of :meth:`narrowed`, and the only
        method that makes a profile wider than it was.

        A grant is the answer to the question the confinement could not previously be asked. The
        profile says a session may write to four directories; the work in front of it needs a
        fifth; and the only other way to say so is to edit the configuration, which is not
        something that can happen inside a turn.

        It takes a :class:`Grant` rather than loose paths on purpose. A grant is minted in one
        place and has to name who approved it, so there is no spelling of "widen this profile"
        that does not carry an authority — the check cannot be skipped by a caller that passes
        paths directly, because there is no such caller.

        **The deny list wins, unconditionally.** A path under `deny` is not widenable by any
        grant, however it was approved — that list is what a person declared never-negotiable
        before any of this started, and a runtime decision must not be able to reach past a
        standing one. This holds for ``whole_disk`` too: it widens to the root, and both backends
        apply the denials over the top of every allowance.

        Nothing here narrows. A grant that names a path already writable is a no-op rather than a
        replacement, which is what makes applying several in sequence safe.
        """
        if grant.whole_disk:
            return replace(
                self,
                filesystem=replace(
                    self.filesystem,
                    readable=tuple(dict.fromkeys(self.filesystem.readable + ("/",))),
                    writable=tuple(dict.fromkeys(self.filesystem.writable + ("/",))),
                ),
                network=True,
            )
        denied = [Path(resolved) for entry in self.filesystem.deny if (resolved := expand(entry, workspace=workspace))]

        def permitted(paths: Iterable[str]) -> tuple[str, ...]:
            kept = []
            for entry in paths:
                resolved = expand(entry, workspace=workspace)
                if not resolved:
                    continue
                candidate = Path(resolved)
                if any(candidate == root or candidate.is_relative_to(root) for root in denied):
                    continue
                kept.append(entry)
            return tuple(kept)

        granted_reads = permitted(grant.reads)
        granted_writes = permitted(grant.writes)
        return replace(
            self,
            filesystem=Filesystem(
                # A granted write implies the read that goes with it. Every tool that writes a
                # file reads it first — `edit_file` must, and a shell redirect into an existing
                # file opens it — so a grant that gave the write alone would be approved, applied,
                # and then fail on the read half in a way nobody could explain from the profile.
                readable=tuple(dict.fromkeys(self.filesystem.readable + granted_reads + granted_writes)),
                writable=tuple(dict.fromkeys(self.filesystem.writable + granted_writes)),
                deny=self.filesystem.deny,
                grantable=self.filesystem.grantable,
                # Carried, not rebuilt. A grant and an attachment are independent routes, and
                # applying one must not quietly revoke the other.
                attached=self.filesystem.attached,
            ),
            network=self.network or grant.network,
        )

    def with_attachments(self, paths: Iterable[str]) -> "Profile":
        """This profile plus read access to exactly the files a person attached.

        Not a grant, and deliberately a separate route. A grant answers a request the *model*
        made, so `deny` must beat it — a model asking to read `~/Documents/tax.pdf` is the
        attack that list exists for. An attachment is the opposite direction: a person picked
        one file in the interface and handed it over. Nothing the model does can put a path
        here, because no tool attaches a file.

        So `deny` does not apply, and the allowance is per file. Somebody who attaches
        `~/Downloads/report.pdf` has opened that document and nothing else — not the folder,
        not its siblings.
        """
        files = tuple(path for path in paths if path)
        if not files:
            return self
        return replace(
            self,
            filesystem=replace(
                self.filesystem,
                attached=tuple(dict.fromkeys(self.filesystem.attached + files)),
            ),
        )

    def may_read(self, path: str, *, workspace: str = "") -> bool:
        """Whether a child of this profile could read ``path``.

        The harness writes and reads files two ways: through a shell command, which runs in a
        confined child, and through its own file tools, which run in the worker process where no
        sandbox applies. Both are the session reaching the disk, so both answer to this.

        The order matches what the backends emit — allowances, then denials, then the files a
        person attached by hand, which beat the denials because a person named them one by one.
        """
        resolved = expand(path, workspace=workspace)
        if not resolved:
            return False
        if self._is_attached(resolved, workspace=workspace):
            return True
        if self._is_denied(resolved, workspace=workspace):
            return False
        # The workspace is readable whatever else is configured: a session pointed at a
        # directory to work in must be able to read it, or it is not restricted but broken.
        if workspace and _within(resolved, expand("$WORKSPACE", workspace=workspace)):
            return True
        if _outside_home(resolved):
            return True
        return any(
            _within(resolved, expand(entry, workspace=workspace))
            for entry in self.filesystem.readable + self.filesystem.writable
        )

    def may_write(self, path: str, *, workspace: str = "") -> bool:
        """Whether a child of this profile could write ``path``. Writes are denied wholesale and
        granted back, so only the writable list — never the system, never an attachment."""
        resolved = expand(path, workspace=workspace)
        if not resolved or self._is_denied(resolved, workspace=workspace):
            return False
        return any(
            _within(resolved, expand(entry, workspace=workspace))
            for entry in self.filesystem.writable
        )

    def _is_denied(self, resolved: str, *, workspace: str) -> bool:
        return any(
            _within(resolved, expand(entry, workspace=workspace))
            for entry in self.filesystem.deny
        )

    def _is_attached(self, resolved: str, *, workspace: str) -> bool:
        return any(
            expand(entry, workspace=workspace) == resolved for entry in self.filesystem.attached
        )

    def grants_without_asking(self, paths: Iterable[str], *, workspace: str = "") -> bool:
        """Whether every one of ``paths`` lies inside what a person already said may be granted.

        The quiet path, and it is deliberately all-or-nothing: a request that reaches one listed
        path and one unlisted one is asked about whole. Approving the half that was pre-cleared
        and gating the rest would split one intent into two decisions and show the person a
        request that no longer matches what the agent asked for."""
        listed = tuple(paths)
        if not listed or not self.filesystem.grantable:
            return False
        return len(_contained_in(listed, self.filesystem.grantable, workspace=workspace)) == len(listed)

    def as_dict(self) -> dict:
        """The form that travels to a worker and out to a client."""
        return {
            "filesystem": {
                "readable": list(self.filesystem.readable),
                "writable": list(self.filesystem.writable),
                "deny": list(self.filesystem.deny),
                "grantable": list(self.filesystem.grantable),
            },
            "network": self.network,
            "limits": dict(self.limits),
            "umask": self.umask,
            "nice": self.nice,
            "enforce": self.enforce,
            # Empty in every profile the configuration can currently express, and carried anyway.
            # This dict is what travels to a worker and what a session record stores, so a field
            # left out of it is a field that silently reverts the moment a profile crosses either
            # boundary — and `child_environment` builds every confined child's environment out of
            # this one.
            "environment": dict(self.environment),
        }

    def describe(self, *, workspace: str = "") -> dict:
        """This profile as the model is told it: resolved paths, and nothing it cannot act on.

        Resolved rather than written the way the configuration writes it, because the whole
        confusion this exists to end is that `$TMPDIR` is not `/tmp`. A model asked whether it
        may write to `/tmp` cannot expand a shell variable in its head, and one shown
        `$TMPDIR` in a list will read it as covering the obvious temporary directory. It does
        not: on macOS it expands to a per-user path under `/var/folders`.

        The system is readable and is deliberately absent, for the reason the configuration
        gives about its own defaults — `/usr` and `/etc` are not secrets, and listing them
        would bury the handful of entries that decide anything.
        """
        backend = backend_name()
        if not backend:
            # Nothing on this machine can enforce a path. To list writable, readable and denied
            # directories here would describe a boundary that does not exist — the worst kind of
            # thing to tell a model, because it reads exactly like a real one and would have it
            # route around a wall that is not there, or ask for access it already holds.
            #
            # `enforce` decides whether a session may run at all in this state; that refusal
            # happens at creation. What this says is only what is true now.
            return {"enforced": False}

        def resolved(entries: Iterable[str]) -> list[str]:
            seen: list[str] = []
            for entry in entries:
                path = expand_for_display(entry, workspace=workspace)
                if path and path not in seen:
                    seen.append(path)
            return seen

        return {
            "writable": resolved(self.filesystem.writable),
            "readable": resolved(self.filesystem.readable),
            "denied": resolved(self.filesystem.deny),
            "network": self.network,
            "enforced_by": backend,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Profile":
        if not data:
            return cls()
        filesystem = data.get("filesystem") or {}
        umask = data.get("umask")
        return cls(
            filesystem=Filesystem(
                readable=tuple(filesystem.get("readable") or ()),
                writable=tuple(filesystem.get("writable") or ()),
                deny=tuple(filesystem.get("deny") or ()),
                grantable=tuple(filesystem.get("grantable") or ()),
            ),
            network=bool(data.get("network", False)),
            limits={str(key): int(value) for key, value in (data.get("limits") or {}).items()},
            umask=int(umask) if umask is not None else None,
            nice=int(data.get("nice") or 0),
            enforce=str(data.get("enforce") or ENFORCE_REQUIRED),
            environment={str(key): str(value) for key, value in (data.get("environment") or {}).items()},
        )


def expand(path: str, *, workspace: str = "") -> str:
    """One path from the configuration as an absolute path on this machine.

    ``$WORKSPACE`` is the session's own directory, which is not known until the session exists;
    the rest is ordinary shell expansion so a person writes what they would write anywhere else."""
    text = path.replace("$WORKSPACE", workspace or "")
    text = os.path.expandvars(os.path.expanduser(text))
    if not text or "$" in text:
        return ""
    try:
        return str(Path(text).resolve(strict=False))
    except OSError:
        return ""


def expand_for_display(path: str, *, workspace: str = "") -> str:
    """One configured path as a person or a model would write it, not as the kernel stores it.

    The same expansion as :func:`expand` — `$WORKSPACE`, environment variables, `~` — but it
    stops short of following symbolic links. That last step is right for enforcement and wrong
    for display, and on macOS the difference is the whole point: `/tmp` is a symlink to
    `/private/tmp`, so a resolved listing tells a model that it may write to `/private/tmp` and
    says nothing about the path it was actually going to use.

    The model would then read the list, fail to find `/tmp`, and either ask for access it
    already holds or route around a wall that is not there. Both are the confusion this listing
    exists to end, reintroduced by the formatting.
    """
    text = path.replace("$WORKSPACE", workspace or "")
    text = os.path.expandvars(os.path.expanduser(text))
    if not text or "$" in text:
        return ""
    return str(Path(text))


def _within(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or lies under it. Both already expanded."""
    if not path or not root:
        return False
    if root == "/":
        return True
    candidate, parent = Path(path), Path(root)
    return candidate == parent or candidate.is_relative_to(parent)


def _outside_home(resolved: str) -> bool:
    """Whether a path lies outside the user's home directory, which both backends leave
    readable: `/usr` and `/etc` are not secrets, and denying them breaks every toolchain while
    protecting nothing."""
    home = os.path.expanduser("~")
    return not home or home == "/" or not _within(resolved, home)


def _contained_in(paths: Iterable[str], allowed: Iterable[str], *, workspace: str = "") -> tuple[str, ...]:
    """The paths that lie within ``allowed``. An empty allowance permits nothing, which is what
    makes the clamp a clamp: a parent that may write nowhere gives a child nowhere.

    Both sides are expanded first. They were not, and a parent whose allowance is written the way
    every allowance is written — ``$WORKSPACE``, ``$TMPDIR`` — was compared against a real
    directory, matched nothing, and clamped its child to nowhere. The child then also lost the
    *read* that came with that allowance, which is how a screen-control helper ended up unable to
    execute the interpreter running it."""
    allowed_paths = [Path(resolved) for entry in allowed if (resolved := expand(entry, workspace=workspace))]
    kept = []
    for entry in paths:
        candidate = Path(entry)
        if any(candidate == root or candidate.is_relative_to(root) for root in allowed_paths):
            kept.append(entry)
    return tuple(dict.fromkeys(kept))


# Everything below is the POSIX half: applied in the child between fork and exec, and needing
# no platform code at all.


def _apply_posix(profile: Profile) -> None:
    """Configure the child between fork and exec. Runs in the child, so it must not raise past
    what `subprocess` will report — a failure here would otherwise appear as an unexplained exit."""
    for name in _SUPPORTED_LIMITS:
        value = profile.limits.get(name)
        if value is None:
            continue
        constant = getattr(resource, name, None)
        if constant is None:
            continue  # RLIMIT_NPROC and RLIMIT_AS are not on every platform
        try:
            soft, hard = resource.getrlimit(constant)
            ceiling = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(constant, (ceiling, ceiling))
        except (OSError, ValueError):
            continue
    if profile.umask is not None:
        os.umask(profile.umask)
    if profile.nice:
        try:
            os.nice(profile.nice)
        except OSError:
            pass


def child_environment(profile: Profile, *, workspace: str = "", extra: Optional[dict] = None) -> dict[str, str]:
    """The environment a confined child gets: a base of what makes a process usable, whatever the
    profile added, and nothing else the worker happened to be holding — the model provider's API
    key above all, which no shell command has any reason to see."""
    environment = {key: os.environ[key] for key in _BASE_ENVIRONMENT_KEYS if key in os.environ}
    if workspace:
        environment["PWD"] = workspace
    environment.update(profile.environment)
    environment.update(extra or {})
    return environment


# The macOS backend.

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _quote_sbpl(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _interpreter_roots() -> tuple[str, ...]:
    """The directories a child needs in order to be the Python that was asked for.

    Deduplicated and resolved, because in a virtualenv `sys.prefix` and `sys.base_prefix` differ
    and the child needs both — one for the environment it was launched from, one for the
    interpreter and standard library it actually is."""
    roots = []
    for candidate in (os.path.realpath(sys.executable), sys.prefix, sys.base_prefix):
        resolved = os.path.realpath(candidate)
        if os.path.isfile(resolved):
            resolved = os.path.dirname(resolved)
        if resolved and resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _package_root() -> str:
    """The directory frank itself is imported from.

    A child is launched by file path — `computer/control_child.py` — so it has to be able to read
    the package it lives in. When frank is installed into the environment that is already covered
    by :func:`_interpreter_roots`, because the package sits under `sys.prefix`. From a source
    checkout or an editable install it does not: the package is somewhere under the user's home,
    and the home is denied wholesale a few lines below. The result was that screen control worked
    when frank was installed and failed with "the sandbox refused to run it" for anyone running
    from source, which is every embedder developing against the library.

    The directory the package is imported *from*, not the package directory itself — one level
    further up than it looks. `import frank` has to read the parent to find `frank` in it, and
    granting only `.../src/frank` left the child able to read every file in the package and
    unable to discover that the package was there: `ModuleNotFoundError: No module named 'frank'`,
    from a process standing inside it. The name and the docstring were right and the code was one
    `dirname` short.

    Read-only and execute: the child needs to run the helper, never to modify it."""
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.realpath(os.path.dirname(package))


# The devices every confined child may use, whatever else its profile says. Not a policy choice:
# a sandbox that denies these does not restrict a command, it breaks it — see `build_sbpl`.
_DEVICE_LITERALS: tuple[str, ...] = (
    "/dev/null",
    "/dev/zero",
    "/dev/random",
    "/dev/urandom",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
    "/dev/ptmx",
    # Opened by some macOS runtimes on startup; harmless, and its absence is a hard failure.
    "/dev/dtracehelper",
)


def build_sbpl(profile: Profile, *, workspace: str = "") -> str:
    """The Seatbelt profile for one child.

    Order is the whole game: SBPL is last-match-wins, so the permissive rules are emitted first and
    the denials last. Anything surprising about a profile's behaviour is answered by reading from
    the bottom up."""
    lines = ["(version 1)", "(allow default)"]
    if not profile.network:
        lines.append("(deny network*)")
    # Writes are denied wholesale and then granted back, because the set of places a command should
    # be able to write is small and nameable while the set it should not is the rest of the disk.
    lines.append("(deny file-write*)")
    for entry in profile.filesystem.writable:
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(allow file-write* (subpath {_quote_sbpl(resolved)}))")
    # Reads are the other way round: the system stays readable and the user's home is closed, so
    # only the home needs naming.
    home = os.path.expanduser("~")
    if home and home != "/":
        lines.append(f"(deny file-read* (subpath {_quote_sbpl(home)}))")
    # The workspace itself, always, whatever the mode. A session is pointed at a directory
    # *to work in*; being able to read it is the premise of the job rather than a privilege a
    # mode hands out. A `read_only` profile carries no writable entries and no readable ones —
    # its whole configuration is the absence of them — so the home denial above closed the very
    # tree the session was created to review. Reviewers found `ls` refused in their own
    # workspace, while `read_file` worked on the same files by accident, because the interpreter
    # and package allowances at the end happened to cover the source directory. A read-only
    # session that cannot read is not restricted, it is broken.
    #
    # Before the profile's own denials, so a directory the user refused stays refused even when
    # it sits inside the workspace.
    workspace_root = expand("$WORKSPACE", workspace=workspace) if workspace else ""
    if workspace_root:
        lines.append(f"(allow file-read* (subpath {_quote_sbpl(workspace_root)}))")
    for entry in tuple(profile.filesystem.readable) + tuple(profile.filesystem.writable):
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(allow file-read* (subpath {_quote_sbpl(resolved)}))")
    # Last, so it wins over every allowance above it.
    for entry in profile.filesystem.deny:
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(deny file-read* file-write* (subpath {_quote_sbpl(resolved)}))")
    # After the denials, and therefore beating them — which is the entire point, and is only
    # correct because a person put each of these here by attaching that exact file. `literal`
    # rather than `subpath`: the allowance is the one file and cannot widen to its directory,
    # even if a directory path somehow reached this list.
    for entry in profile.filesystem.attached:
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(allow file-read* (literal {_quote_sbpl(resolved)}))")
    # Metadata everywhere, content nowhere it was not granted. Denying `file-read*` across a
    # home directory also denies `file-read-metadata` on every directory *above* an allowance,
    # and a path cannot be reached without traversing its ancestors — so a subpath allowance
    # under home silently did nothing, including the ones a person writes in their own
    # settings. Metadata is the existence and shape of a file, not its contents; the denial
    # above still refuses every byte, which is what it is for.
    lines.append("(allow file-read-metadata)")
    # Last of all, and therefore winning over every denial above it. A profile that cannot read
    # and execute the interpreter cannot run anything at all, so this is not a preference and not
    # configurable — a home-wide read denial would otherwise silently make the whole sandbox
    # unusable, which is precisely what it did. `sys.executable` is usually a symlink (a
    # virtualenv's `bin/python3`, a managed interpreter under `~/.local/share/uv`) and the sandbox
    # judges the resolved file, so the real prefixes are what get allowed: `sys.prefix` for the
    # environment, `sys.base_prefix` for the interpreter and its standard library.
    for interpreter_root in _interpreter_roots():
        lines.append(f"(allow file-read* process-exec (subpath {_quote_sbpl(interpreter_root)}))")
    # And the package itself, for the same reason: a helper that cannot be read cannot be run.
    lines.append(f"(allow file-read* process-exec (subpath {_quote_sbpl(_package_root())}))")
    # The devices, last of all, and not configurable for the same reason the interpreter is not.
    #
    # `(deny file-write*)` above closes the whole disk and the writable list grants back the few
    # places work happens. `/dev/null` is not one of those places and is not a place at all: it is
    # how a program says "discard this", and a shell writes to it constantly. Git opens it before
    # doing anything and reports `could not open '/dev/null' for reading and writing: Operation
    # not permitted`, which reads like a broken repository rather than a sandbox that forgot the
    # null device. Every ordinary command was failing this way.
    #
    # The rest of the list is the same argument. Entropy comes from `/dev/random`; a redirection
    # or a process substitution goes through `/dev/fd` and the standard streams; anything
    # interactive needs a terminal, which is `/dev/tty` and a pty pair. None of them is a path
    # into the user's data, which is what this profile exists to keep closed.
    for device in _DEVICE_LITERALS:
        lines.append(f"(allow file-read* file-write* (literal {_quote_sbpl(device)}))")
    lines.append('(allow file-read* file-write* (subpath "/dev/fd"))')
    # The pty a terminal is given has a number nobody can predict, so this one is a pattern.
    lines.append('(allow file-read* file-write* (regex #"^/dev/ttys[0-9]+$"))')

    return "\n".join(lines)


def _macos_available() -> bool:
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


# The Linux backend.


# Landlock's syscall numbers. Stable across architectures Linux supports them on, and named here
# because `ctypes` reaches them through `syscall(2)` — there is no libc wrapper to call instead.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_PR_SET_NO_NEW_PRIVS = 38
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000


def _libc():
    """libc with the signatures spelled out.

    `syscall(2)` is variadic, so `ctypes` will default its return to `c_int` and guess at its
    arguments — which truncates a `long` return and can mangle a pointer on the way in. Every
    call below depends on both being right, so both are declared."""
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    # `syscall` is variadic, so only its return type is declared. Declaring `argtypes` would fix
    # one signature for a function that is called here with three different ones; every argument
    # is passed as an explicit ctypes value instead, which is the supported way to call a
    # variadic and the only way to be sure a pointer arrives as a pointer.
    library.syscall.restype = ctypes.c_long
    library.prctl.restype = ctypes.c_int
    library.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    library.unshare.restype = ctypes.c_int
    library.unshare.argtypes = [ctypes.c_int]
    return library, ctypes


def _landlock_available() -> bool:
    """Whether this kernel has Landlock, asked by requesting its ABI version — the call the kernel
    documents for exactly this question, and one that needs no privilege."""
    if sys.platform != "linux":
        return False
    try:
        library, ctypes = _libc()
        version = library.syscall(
            ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.c_void_p(None),
            ctypes.c_size_t(0), ctypes.c_uint32(1),
        )
        return version > 0
    except (OSError, AttributeError, ValueError):
        return False


_LANDLOCK_FS_READ = 0x00008004      # execute | read_file
_LANDLOCK_FS_READ_DIR = 0x00004000  # read_dir
_LANDLOCK_FS_WRITE = 0x0000377A     # write_file, create/remove of every kind, truncate


def _apply_landlock(profile: Profile, workspace: str) -> None:
    """Restrict this process's filesystem, then exec. Runs in the child after fork.

    Landlock restricts rather than remounts, which is why it is the Linux backend: its
    path-beneath rules are the same shape as the configuration, so nothing has to be translated
    into a different model the way a bind-mount view would be."""
    import ctypes

    class RulesetAttribute(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]

    class PathBeneathAttribute(ctypes.Structure):
        # The kernel declares this packed, so it is 12 bytes and not the 16 a naturally-aligned
        # struct would be. Getting that wrong makes every rule silently reject.
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    libc, _ = _libc()
    handled = _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR | _LANDLOCK_FS_WRITE
    attribute = RulesetAttribute(handled_access_fs=handled, handled_access_net=0)
    ruleset = libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET), ctypes.byref(attribute),
        ctypes.c_size_t(ctypes.sizeof(attribute)), ctypes.c_uint32(0),
    )
    if ruleset < 0:
        return

    denied = tuple(
        resolved for entry in profile.filesystem.deny
        if (resolved := expand(entry, workspace=workspace))
    )

    def add(resolved: str, access: int) -> None:
        descriptor = os.open(resolved, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathBeneathAttribute(allowed_access=access, parent_fd=descriptor)
            libc.syscall(
                ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset),
                ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint32(0),
            )
        finally:
            os.close(descriptor)

    def allow(path: str, access: int) -> None:
        """Grant a subtree, minus anything the profile denies inside it.

        Landlock has no denial: a ruleset is allowances, and everything unnamed is already
        refused. That is not enough on its own here, because a denied path can sit inside a
        granted one — `~/.config` is readable by default and `~/.config/something-private` is
        exactly the kind of entry `deny` is for — and a whole-disk grant names the root, under
        which every denied path lies.

        So a granted root that contains a denial is expanded: walk down towards each denial,
        granting the siblings at every level and never granting the denied entry itself. The
        walk is bounded by the number of denied paths, not by the size of the tree.
        """
        resolved = expand(path, workspace=workspace)
        if not resolved or not os.path.exists(resolved):
            return
        root = Path(resolved)
        inside = [Path(entry) for entry in denied if Path(entry).is_relative_to(root)]
        if not inside:
            # The ordinary case, and the one worth keeping cheap: no denial lies under this
            # allowance, so it is granted whole with one rule.
            if any(root.is_relative_to(Path(entry)) for entry in denied):
                return  # the allowance is itself inside a denial
            add(resolved, access)
            return
        # Grant every sibling along the way down, so the subtree is covered except for the
        # denied branches. `frontier` holds directories still to be split.
        frontier = [root]
        while frontier:
            current = frontier.pop()
            blocking = [entry for entry in inside if entry.is_relative_to(current) and entry != current]
            if not blocking:
                if current not in inside and os.path.exists(current):
                    add(str(current), access)
                continue
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child in inside:
                    continue
                if any(entry.is_relative_to(child) for entry in inside):
                    frontier.append(child)
                elif os.path.exists(child):
                    add(str(child), access)

    # The system is readable; the home directory is not named here at all, because Landlock grants
    # rather than denies — anything not granted is already refused.
    for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/dev", "/var"):
        allow(root, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR)
    for entry in profile.filesystem.readable:
        allow(entry, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR)
    for entry in profile.filesystem.writable:
        allow(entry, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR | _LANDLOCK_FS_WRITE)
    # After the denials, and therefore beating them, exactly as on macOS: a person handed this
    # session one named file, and that is not a path the model went looking for.
    for entry in profile.filesystem.attached:
        resolved = expand(entry, workspace=workspace)
        if resolved and os.path.exists(resolved):
            add(resolved, _LANDLOCK_FS_READ)
    # landlock_restrict_self refuses without this: a process may only narrow itself if it cannot
    # then regain privilege through a setuid binary.
    libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)
    )
    os.close(ruleset)


def _unshare_network() -> None:
    """Put the child in an empty network namespace, which is how a Linux process is denied the
    network without privilege: a new user namespace first, because that is what makes the network
    namespace creatable by an ordinary user."""
    libc, _ = _libc()
    libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNET)


# The surface a caller actually uses.


def backend_name() -> str:
    """Which backend can enforce a profile here, or ``""`` when none can."""
    if _macos_available():
        return "sandbox-exec"
    if _landlock_available():
        return "landlock"
    return ""


def describe_backend() -> str:
    """A sentence for a refusal or a log line, naming what is missing and what to do about it."""
    name = backend_name()
    if name:
        return f"{name} on {platform.system()}"
    if sys.platform == "darwin":
        return f"none — {SANDBOX_EXEC} is not present on this macOS"
    if sys.platform == "linux":
        return "none — this kernel has no Landlock (5.13 or newer is needed)"
    return f"none — {platform.system()} has no supported confinement backend"


@dataclass
class Spawn:
    """Everything a caller needs to start a confined child: what to run it through, how to set it
    up in the child, and what environment it gets."""

    prefix: list[str]
    preexec: Callable[[], None]
    environment: dict[str, str]
    confined: bool


@dataclass(frozen=True)
class Attempt:
    """What one child is about to be run with. The only thing :func:`spawn_recipe` accepts.

    A profile alone cannot say whether it has been widened, which leaves every spawn site to
    remember to fold the session's grants in. Making the attempt the argument moves that from
    something each site remembers to something the type states.

    ``grant`` is empty on a first attempt and cannot be otherwise: :func:`first_attempt` takes no
    grant, and it is the only way to build one. That is what makes the retry safe to offer — the
    first run of any command happened inside the box, so whatever it managed to do before the
    operating system stopped it, it did in there."""

    profile: Optional[Profile]
    grant: Optional[Grant] = None
    workspace: str = ""

    @property
    def confined_profile(self) -> Optional[Profile]:
        """The profile actually applied: the session's, with the attempt's grant folded in."""
        if self.profile is None or self.grant is None:
            return self.profile
        return self.profile.with_grant(self.grant, workspace=self.workspace)


def first_attempt(profile: Optional[Profile], *, workspace: str = "") -> Attempt:
    """The way every command starts: inside the session's own box, widened by nothing.

    Takes no grant, so there is no call site that can start a command outside its confinement.
    The session's standing grants are folded into ``profile`` before it gets here — those are
    approvals that already happened, not an escape from this one."""
    return Attempt(profile=profile, grant=None, workspace=workspace)


def retry_attempt(profile: Optional[Profile], grant: Grant, *, workspace: str = "") -> Attempt:
    """A second run of a command the operating system refused, with an approved widening."""
    return Attempt(profile=profile, grant=grant, workspace=workspace)


#: What the two backends say when they refuse. Neither names the path — a Seatbelt denial is an
#: `EPERM` and a Landlock one is an `EACCES` — so this is a reading of the wreckage rather than a
#: report from the enforcer, and it is allowed to be: a false positive costs one needless
#: question, and a false negative leaves today's behaviour, which is the error reaching the model.
_DENIAL_MARKERS = (
    "operation not permitted",
    "permission denied",
    "read-only file system",
    "sandbox",
    "landlock",
    "seatbelt",
)
#: Network refusals, which read differently: no route, no resolver, nothing listening.
_NETWORK_DENIAL_MARKERS = (
    "network is unreachable",
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "connection refused",
    "no address associated with hostname",
)

@dataclass(frozen=True)
class Denial:
    """The operating system refused this child, as far as can be told from what it left behind."""

    kind: str  # "filesystem" | "network"
    evidence: str


def denial_in(*, exit_code: int, output: str, attempt: Attempt) -> Optional[Denial]:
    """Whether a finished child looks like it hit the boundary, and which half of it.

    Deliberately a reading of exit code and output rather than a report from the enforcer,
    because neither backend gives one. What keeps that honest is where the answer is used: it
    decides whether to *ask*, never whether to allow. Being wrong in one direction spends a
    question nobody needed; being wrong in the other returns the error to the model exactly as
    it does today.

    An unconfined attempt is never a denial: if there was no boundary, nothing was refused by it.
    """
    profile = attempt.profile
    if profile is None or profile.enforce == ENFORCE_OFF or exit_code == 0:
        return None
    lowered = output.lower()
    for marker in _NETWORK_DENIAL_MARKERS:
        if marker in lowered:
            # Only where the box is what closed the network. With the network open, an unreachable
            # host is an unreachable host, and offering to widen a permission the command already
            # holds is an offer that fixes nothing.
            return None if profile.network else Denial(kind="network", evidence=marker)
    for marker in _DENIAL_MARKERS:
        if marker in lowered:
            return Denial(kind="filesystem", evidence=marker)
    return None


def spawn_recipe(
    attempt: Attempt,
    *,
    workspace: str = "",
    extra_environment: Optional[dict] = None,
) -> Spawn:
    """Turn an attempt into the arguments a spawn needs.

    Raises :class:`ConfinementUnavailable` when the profile demands enforcement this machine cannot
    provide. That is deliberately noisy: the defect this module exists to correct was a setting
    that claimed to confine and quietly did not."""
    profile = attempt.confined_profile
    if profile is None or profile.enforce == ENFORCE_OFF:
        return Spawn(prefix=[], preexec=lambda: None, environment=dict(os.environ), confined=False)

    backend = backend_name()
    if not backend:
        if profile.enforce == ENFORCE_REQUIRED:
            raise ConfinementUnavailable(
                f"Confinement is required but no backend is available: {describe_backend()}. "
                "Set sandbox.enforce to 'preferred' to run with resource limits only, or 'off' to disable it."
            )
        backend = ""

    environment = child_environment(profile, workspace=workspace, extra=extra_environment)
    prefix: list[str] = []
    posix = _apply_posix

    if backend == "sandbox-exec":
        prefix = [SANDBOX_EXEC, "-p", build_sbpl(profile, workspace=workspace)]

        def setup() -> None:
            posix(profile)

    elif backend == "landlock":

        def setup() -> None:
            posix(profile)
            if not profile.network:
                _unshare_network()
            _apply_landlock(profile, workspace)

    else:

        def setup() -> None:
            posix(profile)

    return Spawn(prefix=prefix, preexec=setup, environment=environment, confined=bool(backend))


def temporary_directory(profile: Optional[Profile], *, workspace: str = "") -> str:
    """A directory this profile permits writing *scratch* to, or ``""`` when it permits none.

    The bash tool writes its own log, and a log written outside the profile would make the tool
    fail on its own bookkeeping rather than on anything the user asked for — so the answer is
    drawn from the profile rather than from the system.

    The workspace is considered last, and that is the whole point of the ordering. This used to
    return the first writable entry in declaration order, and ``$WORKSPACE`` is first in every
    default profile — so every command dropped a ``bash-<id>.log`` into the user's source tree,
    where it turned up in ``git status``, invited an accidental commit, and had to be swept by
    hand. Scratch belongs somewhere scratch is thrown away; the tree the agent is editing is the
    one place it must not accumulate.

    Empty rather than `tempfile.gettempdir()` when nothing qualifies, deliberately. Falling back
    to the system temporary directory would hand a caller a path the profile denies, and every
    caller would then be confined to less than the directory it had just been told to use."""
    if profile is None or profile.enforce == ENFORCE_OFF:
        return tempfile.gettempdir()

    def usable(entry: str) -> str:
        resolved = expand(entry, workspace=workspace)
        return resolved if resolved and os.path.isdir(resolved) and os.access(resolved, os.W_OK) else ""

    worktree_root = os.path.realpath(workspace) if workspace else ""

    def inside_workspace(path: str) -> bool:
        if not worktree_root:
            return False
        real = os.path.realpath(path)
        return real == worktree_root or real.startswith(worktree_root + os.sep)

    candidates = [usable(entry) for entry in profile.filesystem.writable]
    outside = [path for path in candidates if path and not inside_workspace(path)]
    return outside[0] if outside else next((path for path in candidates if path), "")


def private_scratch(profile: Optional[Profile], *, workspace: str = "", prefix: str = "frank-") -> str:
    """A fresh directory of a child's own, or ``""`` when the profile permits nowhere suitable.

    :func:`temporary_directory` answers a different question — *somewhere* this profile may write —
    and it falls back to the workspace when nothing else qualifies, which is right for the bash
    tool's log and wrong for a child that is being narrowed down to scratch. Narrowing a child to
    "the workspace" is not narrowing it: it is handing the user's source tree to a process whose
    whole point is that it needs nothing but somewhere to put a temporary file.

    That failure inverted with configuration, which is what makes it worth its own function. The
    shipped profile lists ``$TMPDIR`` among its writable paths, so the fallback never fired and the
    child was correctly confined; a person who *hardened* their profile down to ``$WORKSPACE``
    alone — the obvious way to tighten it — removed the only candidate outside the tree and thereby
    widened the child to the whole of it. Nothing about that is visible from either setting.

    So the workspace is refused outright here rather than preferred last, and a fresh subdirectory
    is made inside whatever does qualify: two children of one session cannot see each other's
    scratch, and no child can write the tree. Empty when nothing qualifies, which leaves the child
    unable to write anywhere at all — the correct answer for one that only bridges."""
    base = temporary_directory(profile, workspace=workspace)
    if not base:
        return ""
    worktree_root = os.path.realpath(workspace) if workspace else ""
    if worktree_root:
        real = os.path.realpath(base)
        if real == worktree_root or real.startswith(worktree_root + os.sep):
            return ""
    try:
        return tempfile.mkdtemp(prefix=prefix, dir=base)
    except OSError:
        return ""


def resolve_command(command: str, spawn: Spawn) -> list[str]:
    """The argv for a shell command under a spawn recipe. A confined command is still a shell
    command — the prefix wraps the shell, it does not replace it."""
    shell = shutil.which("bash") or "/bin/sh"
    return [*spawn.prefix, shell, "-c", command]


def probe() -> dict:
    """Whether confinement actually works here, checked rather than assumed.

    Run once at daemon start. On macOS this executes a trivial profile, because the presence of
    `sandbox-exec` on disk and Apple's willingness to honour it are different questions and the
    deprecation makes the second one worth asking every boot."""
    name = backend_name()
    if name == "sandbox-exec":
        try:
            finished = subprocess.run(
                [SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/bin/echo", "ok"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            working = finished.returncode == 0 and "ok" in finished.stdout
        except (OSError, subprocess.SubprocessError):
            working = False
        return {"backend": name if working else "", "detail": describe_backend() if working else
                f"{SANDBOX_EXEC} is present but did not run a trivial profile"}
    return {"backend": name, "detail": describe_backend()}
