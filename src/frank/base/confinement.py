"""What a tool's child process is actually allowed to do, enforced by the operating system."""

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

# How a missing backend is handled.
ENFORCE_REQUIRED = "required"
ENFORCE_PREFERRED = "preferred"
ENFORCE_OFF = "off"

# The rlimits the configuration may name.
_SUPPORTED_LIMITS = ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_CORE", "RLIMIT_NOFILE")

# Environment variables a child needs to be a usable Unix process.
_BASE_ENVIRONMENT_KEYS = (
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL", "TERM", "TZ", "USER",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
)


class ConfinementUnavailable(RuntimeError):
    """No backend can enforce a profile on this machine, and the policy says that is fatal."""


@dataclass(frozen=True)
class Filesystem:
    """Which paths a child may read and which it may write."""

    readable: tuple[str, ...] = ()
    writable: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    # Paths a runtime grant may open without asking a person.
    grantable: tuple[str, ...] = ()
    # Exact files the person handed to this session, and the one thing that outranks `deny`.
    attached: tuple[str, ...] = ()

    def intersect(self, parent: "Filesystem") -> "Filesystem":
        """This filesystem clamped against a parent's: never wider, and the parent's denials are inherited whole."""
        return Filesystem(
            readable=_contained_in(self.readable, parent.readable),
            writable=_contained_in(self.writable, parent.writable),
            deny=tuple(dict.fromkeys(self.deny + parent.deny)),
            # What may be granted without asking narrows exactly as an allowance does: a child cannot be handed a quieter approval path than its parent holds.
            grantable=_contained_in(self.grantable, parent.grantable),
            # Deliberately dropped.
            attached=(),
        )


@dataclass(frozen=True)
class AccessRequest:
    """What one call says it needs beyond the confinement it already has."""

    mutates: Optional[bool] = None
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    network: bool = False

    @property
    def wants_widening(self) -> bool:
        """Whether this asks for anything at all."""
        return bool(self.reads or self.writes or self.network)


def parse_access_request(value: object) -> tuple[Optional[AccessRequest], str]:
    """One tool argument as an :class:`AccessRequest`, or a sentence saying why it is not."""
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
        # A request for the whole filesystem is not a request; it is a way of not being asked again.
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
    """One widening that was approved, what it was approved for, and who approved it."""

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


#: Who approved a grant.
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
    """Mint a grant. The one place a :class:`Grant` is built outside deserialization."""
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
    # Closed, like the filesystem it sits beside.
    network: bool = False
    limits: dict[str, int] = field(default_factory=dict)
    umask: Optional[int] = None
    nice: int = 0
    environment: dict[str, str] = field(default_factory=dict)
    enforce: str = ENFORCE_REQUIRED

    def clamp(self, parent: Optional["Profile"]) -> "Profile":
        """This profile as a child of ``parent`` — never wider on any axis."""
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
        """A stricter variant of this profile, for a child that needs less than the session does."""
        return replace(
            self,
            filesystem=Filesystem(
                readable=self.filesystem.readable,
                # A strict intersection, with no fallback.
                writable=_contained_in(tuple(writable), self.filesystem.writable, workspace=workspace),
                deny=self.filesystem.deny,
                # Explicitly nothing.
                grantable=(),
            ),
            network=self.network and network,
        )

    def with_grant(self, grant: Grant, *, workspace: str = "") -> "Profile":
        """This profile plus one approved widening."""
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
                # A granted write implies the read that goes with it.
                readable=tuple(dict.fromkeys(self.filesystem.readable + granted_reads + granted_writes)),
                writable=tuple(dict.fromkeys(self.filesystem.writable + granted_writes)),
                deny=self.filesystem.deny,
                grantable=self.filesystem.grantable,
                # Carried, not rebuilt.
                attached=self.filesystem.attached,
            ),
            network=self.network or grant.network,
        )

    def with_attachments(self, paths: Iterable[str]) -> "Profile":
        """This profile plus read access to exactly the files a person attached."""
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
        """Whether a child of this profile could read ``path``."""
        resolved = expand(path, workspace=workspace)
        if not resolved:
            return False
        if self._is_attached(resolved, workspace=workspace):
            return True
        if self._is_denied(resolved, workspace=workspace):
            return False
        # The workspace is readable whatever else is configured: a session pointed at a directory to work in must be able to read it, or it is not restricted but broken.
        if workspace and _within(resolved, expand("$WORKSPACE", workspace=workspace)):
            return True
        if _outside_home(resolved):
            return True
        return any(
            _within(resolved, expand(entry, workspace=workspace))
            for entry in self.filesystem.readable + self.filesystem.writable
        )

    def may_write(self, path: str, *, workspace: str = "") -> bool:
        """Whether a child of this profile could write ``path``."""
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
        """Whether every one of ``paths`` lies inside what a person already said may be granted."""
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
            "environment": dict(self.environment),
        }

    def describe(self, *, workspace: str = "") -> dict:
        """This profile as the model is told it: resolved paths, and nothing it cannot act on."""
        backend = backend_name()
        if not backend:
            # Nothing on this machine can enforce a path.
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
    """One path from the configuration as an absolute path on this machine."""
    text = path.replace("$WORKSPACE", workspace or "")
    text = os.path.expandvars(os.path.expanduser(text))
    if not text or "$" in text:
        return ""
    try:
        return str(Path(text).resolve(strict=False))
    except OSError:
        return ""


def expand_for_display(path: str, *, workspace: str = "") -> str:
    """One configured path as a person or a model would write it, not as the kernel stores it."""
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
    """Whether a path lies outside the user's home directory, which both backends leave readable: `/usr` and `/etc` are not secrets, and denying them breaks every toolchain while protecting nothing."""
    home = os.path.expanduser("~")
    return not home or home == "/" or not _within(resolved, home)


def _contained_in(paths: Iterable[str], allowed: Iterable[str], *, workspace: str = "") -> tuple[str, ...]:
    """The paths that lie within ``allowed``."""
    allowed_paths = [Path(resolved) for entry in allowed if (resolved := expand(entry, workspace=workspace))]
    kept = []
    for entry in paths:
        candidate = Path(entry)
        if any(candidate == root or candidate.is_relative_to(root) for root in allowed_paths):
            kept.append(entry)
    return tuple(dict.fromkeys(kept))


# Everything below is the POSIX half: applied in the child between fork and exec, and needing no platform code at all.


def _apply_posix(profile: Profile) -> None:
    """Configure the child between fork and exec."""
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
    """The environment a confined child gets: a base of what makes a process usable, whatever the profile added, and nothing else the worker happened to be holding — the model provider's API key above all, which no shell command has any reason to see."""
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
    """The directories a child needs in order to be the Python that was asked for."""
    roots = []
    for candidate in (os.path.realpath(sys.executable), sys.prefix, sys.base_prefix):
        resolved = os.path.realpath(candidate)
        if os.path.isfile(resolved):
            resolved = os.path.dirname(resolved)
        if resolved and resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _package_root() -> str:
    """The directory frank itself is imported from."""
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.realpath(os.path.dirname(package))


# The devices every confined child may use, whatever else its profile says.
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
    """The Seatbelt profile for one child."""
    lines = ["(version 1)", "(allow default)"]
    if not profile.network:
        lines.append("(deny network*)")
    # Writes are denied wholesale and then granted back, because the set of places a command should be able to write is small and nameable while the set it should not is the rest of the disk.
    lines.append("(deny file-write*)")
    for entry in profile.filesystem.writable:
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(allow file-write* (subpath {_quote_sbpl(resolved)}))")
    # Reads are the other way round: the system stays readable and the user's home is closed, so only the home needs naming.
    home = os.path.expanduser("~")
    if home and home != "/":
        lines.append(f"(deny file-read* (subpath {_quote_sbpl(home)}))")
    # The workspace itself, always, whatever the mode.
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
    # After the denials, and therefore beating them — which is the entire point, and is only correct because a person put each of these here by attaching that exact file.
    for entry in profile.filesystem.attached:
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(allow file-read* (literal {_quote_sbpl(resolved)}))")
    # Metadata everywhere, content nowhere it was not granted.
    lines.append("(allow file-read-metadata)")
    # Last of all, and therefore winning over every denial above it.
    for interpreter_root in _interpreter_roots():
        lines.append(f"(allow file-read* process-exec (subpath {_quote_sbpl(interpreter_root)}))")
    # And the package itself, for the same reason: a helper that cannot be read cannot be run.
    lines.append(f"(allow file-read* process-exec (subpath {_quote_sbpl(_package_root())}))")
    # The devices, last of all, and not configurable for the same reason the interpreter is not.
    for device in _DEVICE_LITERALS:
        lines.append(f"(allow file-read* file-write* (literal {_quote_sbpl(device)}))")
    lines.append('(allow file-read* file-write* (subpath "/dev/fd"))')
    # The pty a terminal is given has a number nobody can predict, so this one is a pattern.
    lines.append('(allow file-read* file-write* (regex #"^/dev/ttys[0-9]+$"))')

    return "\n".join(lines)


def _macos_available() -> bool:
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


# The Linux backend.


# Landlock's syscall numbers.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_PR_SET_NO_NEW_PRIVS = 38
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNET = 0x40000000


def _libc():
    """libc with the signatures spelled out."""
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    # `syscall` is variadic, so only its return type is declared.
    library.syscall.restype = ctypes.c_long
    library.prctl.restype = ctypes.c_int
    library.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    library.unshare.restype = ctypes.c_int
    library.unshare.argtypes = [ctypes.c_int]
    return library, ctypes


def _landlock_available() -> bool:
    """Whether this kernel has Landlock, asked by requesting its ABI version — the call the kernel documents for exactly this question, and one that needs no privilege."""
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
    """Restrict this process's filesystem, then exec. Runs in the child after fork."""
    import ctypes

    class RulesetAttribute(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]

    class PathBeneathAttribute(ctypes.Structure):
        # The kernel declares this packed, so it is 12 bytes and not the 16 a naturally-aligned struct would be.
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
        """Grant a subtree, minus anything the profile denies inside it."""
        resolved = expand(path, workspace=workspace)
        if not resolved or not os.path.exists(resolved):
            return
        root = Path(resolved)
        inside = [Path(entry) for entry in denied if Path(entry).is_relative_to(root)]
        if not inside:
            # The ordinary case, and the one worth keeping cheap: no denial lies under this allowance, so it is granted whole with one rule.
            if any(root.is_relative_to(Path(entry)) for entry in denied):
                return  # the allowance is itself inside a denial
            add(resolved, access)
            return
        # Grant every sibling along the way down, so the subtree is covered except for the denied branches.
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

    # The system is readable; the home directory is not named here at all, because Landlock grants rather than denies — anything not granted is already refused.
    for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/dev", "/var"):
        allow(root, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR)
    for entry in profile.filesystem.readable:
        allow(entry, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR)
    for entry in profile.filesystem.writable:
        allow(entry, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR | _LANDLOCK_FS_WRITE)
    # After the denials, and therefore beating them, exactly as on macOS: a person handed this session one named file, and that is not a path the model went looking for.
    for entry in profile.filesystem.attached:
        resolved = expand(entry, workspace=workspace)
        if resolved and os.path.exists(resolved):
            add(resolved, _LANDLOCK_FS_READ)
    # landlock_restrict_self refuses without this: a process may only narrow itself if it cannot then regain privilege through a setuid binary.
    libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    libc.syscall(
        ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)
    )
    os.close(ruleset)


def _unshare_network() -> None:
    """Put the child in an empty network namespace, which is how a Linux process is denied the network without privilege: a new user namespace first, because that is what makes the network namespace creatable by an ordinary user."""
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
    """Everything a caller needs to start a confined child: what to run it through, how to set it up in the child, and what environment it gets."""

    prefix: list[str]
    preexec: Callable[[], None]
    environment: dict[str, str]
    confined: bool


@dataclass(frozen=True)
class Attempt:
    """What one child is about to be run with. The only thing :func:`spawn_recipe` accepts."""

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
    """The way every command starts: inside the session's own box, widened by nothing."""
    return Attempt(profile=profile, grant=None, workspace=workspace)


def retry_attempt(profile: Optional[Profile], grant: Grant, *, workspace: str = "") -> Attempt:
    """A second run of a command the operating system refused, with an approved widening."""
    return Attempt(profile=profile, grant=grant, workspace=workspace)


#: What the two backends say when they refuse.
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
    """Whether a finished child looks like it hit the boundary, and which half of it."""
    profile = attempt.profile
    if profile is None or profile.enforce == ENFORCE_OFF or exit_code == 0:
        return None
    lowered = output.lower()
    for marker in _NETWORK_DENIAL_MARKERS:
        if marker in lowered:
            # Only where the box is what closed the network.
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
    """Turn an attempt into the arguments a spawn needs."""
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
    """A directory this profile permits writing *scratch* to, or ``""`` when it permits none."""
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
    """A fresh directory of a child's own, or ``""`` when the profile permits nowhere suitable."""
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
    """The argv for a shell command under a spawn recipe."""
    shell = shutil.which("bash") or "/bin/sh"
    return [*spawn.prefix, shell, "-c", command]


def probe() -> dict:
    """Whether confinement actually works here, checked rather than assumed."""
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
