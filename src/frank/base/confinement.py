"""What a tool's child process is actually allowed to do, enforced by the operating system.

The harness spawns two kinds of child that run code nobody wrote in advance: the shell command a
`bash` call asks for, and the Python a `control_screen` script is made of. Both used to be
"protected" by something that was not a boundary — the first by a scan of the command text for
path-shaped arguments, the second by ``RLIMIT_CPU`` and ``RLIMIT_AS``. A heuristic over source is
not confinement, and bounding runaway resource use is not bounding authority. This module is the
boundary both of them get instead.

Almost all of a confined child is POSIX and needs no platform code: resource limits are
``setrlimit(2)`` under their own constant names, the file-creation mask is ``umask(2)``, priority
is ``nice(2)``, and the environment is built rather than inherited. All four are applied between
fork and exec, which is where a Unix process has always configured itself.

Two things have no POSIX spelling — which files a process may touch, and whether it may reach the
network — and they are the two that matter. macOS answers with `sandbox-exec` and a generated SBPL
profile; Linux answers with Landlock for the filesystem and a network namespace for the network.
The macOS interface is deprecated by Apple and depended on anyway, because the alternatives either
need privileges the harness does not have or take away the user's own files, which is the thing the
harness exists to reach. See `documentation/plans/confinement.md` for what was rejected and why.

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

    def intersect(self, parent: "Filesystem") -> "Filesystem":
        """This filesystem clamped against a parent's: never wider, and the parent's denials are
        inherited whole. Path containment rather than string equality, so a child asking for a
        subdirectory of what its parent holds keeps it."""
        return Filesystem(
            readable=_contained_in(self.readable, parent.readable),
            writable=_contained_in(self.writable, parent.writable),
            deny=tuple(dict.fromkeys(self.deny + parent.deny)),
        )


@dataclass(frozen=True)
class Profile:
    """One child's confinement, whole. Frozen because it is resolved once and then only narrowed."""

    filesystem: Filesystem = field(default_factory=Filesystem)
    network: bool = True
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
            ),
            network=self.network and network,
        )

    def as_dict(self) -> dict:
        """The form that travels to a worker and out to a client."""
        return {
            "filesystem": {
                "readable": list(self.filesystem.readable),
                "writable": list(self.filesystem.writable),
                "deny": list(self.filesystem.deny),
            },
            "network": self.network,
            "limits": dict(self.limits),
            "umask": self.umask,
            "nice": self.nice,
            "enforce": self.enforce,
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
            ),
            network=bool(data.get("network", True)),
            limits={str(key): int(value) for key, value in (data.get("limits") or {}).items()},
            umask=int(umask) if umask is not None else None,
            nice=int(data.get("nice") or 0),
            enforce=str(data.get("enforce") or ENFORCE_REQUIRED),
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
    for entry in tuple(profile.filesystem.readable) + tuple(profile.filesystem.writable):
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(allow file-read* (subpath {_quote_sbpl(resolved)}))")
    # Last, so it wins over every allowance above it.
    for entry in profile.filesystem.deny:
        resolved = expand(entry, workspace=workspace)
        if resolved:
            lines.append(f"(deny file-read* file-write* (subpath {_quote_sbpl(resolved)}))")
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

    def allow(path: str, access: int) -> None:
        resolved = expand(path, workspace=workspace)
        if not resolved or not os.path.exists(resolved):
            return
        descriptor = os.open(resolved, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathBeneathAttribute(allowed_access=access, parent_fd=descriptor)
            libc.syscall(
                ctypes.c_long(_SYS_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset),
                ctypes.c_int(1), ctypes.byref(rule), ctypes.c_uint32(0),
            )
        finally:
            os.close(descriptor)

    # The system is readable; the home directory is not named here at all, because Landlock grants
    # rather than denies — anything not granted is already refused.
    for root in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/proc", "/dev", "/var"):
        allow(root, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR)
    for entry in profile.filesystem.readable:
        allow(entry, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR)
    for entry in profile.filesystem.writable:
        allow(entry, _LANDLOCK_FS_READ | _LANDLOCK_FS_READ_DIR | _LANDLOCK_FS_WRITE)
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


def spawn_recipe(
    profile: Optional[Profile],
    *,
    workspace: str = "",
    extra_environment: Optional[dict] = None,
) -> Spawn:
    """Turn a profile into the arguments a spawn needs.

    Raises :class:`ConfinementUnavailable` when the profile demands enforcement this machine cannot
    provide. That is deliberately noisy: the defect this module exists to correct was a setting
    that claimed to confine and quietly did not."""
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
