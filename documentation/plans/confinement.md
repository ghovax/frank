---
created: 2026-07-25T16:06:37Z
updated: 2026-07-25T16:22:00Z
commit: c36b467
---

# The Sandbox That Confines Nothing

`sandbox: { enabled: true }` is in the configuration, in the settings API, and in the documentation, where it is described as confining bash to the active workspace. It does not confine anything. What it does is split the command on shell separators, `shlex` the pieces, look for arguments that resemble paths, resolve them, and — if any land outside the working directory — raise an approval prompt. On approval the command runs with the user's full privileges, unrestricted. `cat $(echo /etc/passwd)` never produces a path-shaped token and sails through; so does any interpreter, any subshell, any command that builds its argument at runtime. It is a heuristic over source text presented as a boundary.

The same mistake, in a different shape, protects the `control_screen` child. That child executes model-authored Python, and its own module docstring says a runaway loop or crash "dies with it and never touches the server" — true of crashes and false of intent, because the only limits applied are `RLIMIT_CPU` and `RLIMIT_AS`. Bounding runaway resource use is not bounding authority. The permission classifier that gated it was recently rewritten to stop assuming a script is inert unless it names a primitive, which closed the specific route through `import os`; but that fix is a gate over source, and source can be obscured. A gate is not a boundary, and this plan is about the boundary.

Two children, then, spawned in two different modules, protected by two different things, neither of which is confinement. They should share one mechanism, and that mechanism should be the operating system's, not ours.

## What a confined child is made of

Almost all of it is POSIX, and needs no platform code at all. Resource limits are `setrlimit(2)`, and the configuration names them by their own constants — `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NPROC`, `RLIMIT_CORE` — taking the integers `setrlimit` itself takes, because a friendlier `4Gi` spelling would be a convention invented at the configuration layer for a call that has never accepted one. The file-creation mask is `umask(2)`. Scheduling priority is `nice(2)`. The environment is built rather than inherited, so a child gets what it needs instead of everything the worker happened to hold. All four are applied in a `preexec` hook between fork and exec, which is where a Unix process has always configured itself.

Two things have no POSIX spelling: which files a process may read and write, and whether it may reach the network. Those are the only parts that need a backend, and they are the parts that matter most.

On macOS the mechanism is `sandbox-exec` with a generated SBPL profile. It is the only per-command sandbox the platform offers, it is present on every current release, and it is what browsers and other agent harnesses use in production. It is also deprecated, and this plan treats it as deprecated rather than pretending otherwise: the module says so, the documentation says so, and replacing it is listed below as the future direction it is. Rule order in SBPL is last-match-wins, so the generator emits the permissive rules first and the denials last, and the denial list is what a reader should check when a profile behaves unexpectedly.

On Linux the filesystem is Landlock, applied through `ctypes` because there is no stdlib binding, and network denial is `unshare(CLONE_NEWUSER|CLONE_NEWNET)`. Landlock is chosen over `bubblewrap` for a reason worth stating: Landlock's path-beneath rules are the same shape as the configuration, so the translation is direct, while bubblewrap's model is bind mounts, which would mean expressing "deny this subtree" as "do not bind it" and rebuilding a filesystem view rather than restricting one. It also avoids depending on an external binary being installed.

## Why `sandbox-exec`, when Apple says not to

The decision is to depend on it, and it is worth writing down why, because the deprecation notice is the first thing anyone reviewing this will find. On macOS the choice is binary: `sandbox-exec` is the mechanism, or there is no mechanism and the platform gets the POSIX half only — resource limits, mask, priority, a scoped environment — which is hygiene rather than a boundary. Since macOS is the platform this harness actually targets, that would mean writing a confinement design whose central promise does not apply where it runs.

| Mechanism | Confines files and network | Applies to one child | Needs root | Why not |
|---|---|---|---|---|
| `sandbox-exec` (Seatbelt, SBPL) | Yes | Yes | No | **Chosen.** Deprecated by Apple since 10.15 |
| App Sandbox (entitlements) | Yes | No — the whole signed application | No | Would confine the harness itself out of the user's files, which is the thing it exists to reach |
| Endpoint Security | No — it observes | n/a | Apple-granted entitlement | An auditing interface, not a boundary |
| A separate uid per session | Yes, through ordinary file modes | Per session | Yes, to provision | The agent could no longer read the user's repository or use their credentials — it stops acting as the user |
| `chroot` | Partly | Yes | Yes | Same loss as above, and requires privilege the harness does not have |
| A container | Yes | Yes | No | No access to the user's real files or logins, and it cannot drive the browser the user is signed into |

The deprecation is a known risk with a bounded blast radius rather than a reason to stop. It was marked deprecated around 10.15 and has shipped and worked in every release since; Chrome's renderer sandbox is built on Seatbelt, as are other agent harnesses. If Apple removes it, the capability probe fails, confinement degrades to the `enforce` setting's chosen behaviour, and nothing else in this design changes — which is precisely why the probe and the `enforce` levels exist rather than being an afterthought.

This reasoning belongs in the shipped documentation too, not only here. A person reading `configuration.md` and finding that their agent harness depends on a deprecated Apple interface deserves to find the alternatives and their costs in the same place, rather than concluding it was chosen carelessly.

## Confinement belongs to the session

The harness already has a property that is decided once, at creation, cannot be widened afterwards, and is clamped so that a child is never looser than its parent: the permission mode. Confinement is the same kind of thing and gets the same treatment. It is resolved at `session.create` from the global configuration and the agent profile, clamped against the creating session, and stored on the session record, which is what the worker receives and applies to every child it spawns.

The clamp is what makes this compose rather than merely exist. Path sets intersect: a child's writable set is the intersection of what was asked for and what the parent holds, so a session can never hand a peer a wider filesystem than its own. Network takes the stricter of the two. Limits take the lower. Without that, a confined session could create an unconfined peer and the boundary would be one `create_session` call deep.

Per-agent overrides sit alongside the per-agent bash rules that already exist, because an investigation agent and a build agent genuinely want different filesystems, and the harness already accepts that agents differ in what they may do.

## The two children want different profiles

The `bash` child runs the user's toolchain and needs most of a working machine: compilers, package managers, git, the network. Its profile is the configured one.

The `control_screen` child needs almost nothing. Every capability it has is bridged to the parent over two pipes — a click, a find, an evaluate all become JSON requests that the parent performs — so the child itself requires no network at all and no filesystem beyond a temporary directory. Its profile is therefore *derived* from the session's and made stricter, rather than being separately configurable: two profiles to configure would be the fragmentation this design is trying to avoid, and there is no case in which a person wants that child to have network.

## What is denied by default

The system stays readable. `/usr`, `/bin`, `/etc` and their equivalents are not secrets, and denying them breaks every command while protecting nothing.

The user's home is denied by default with an allowlist, and the allowlist is generous on purpose. Credential and configuration directories — `~/.ssh`, `~/.gitconfig`, `~/.config`, `~/.cargo`, `~/.npmrc` and the rest of the dotfile landscape — stay readable, because the tools that need them are working external architecture and breaking `git push` to protect a key is a bad trade made by someone who does not have to use the result. What is denied is the personal data no toolchain touches: `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Pictures`, mail and message stores, and browser profiles. That is where a compromised session's exfiltration would actually hurt, and denying it costs nothing anyone will notice.

Writes are narrower than reads, because a wrong write is the failure people actually experience: the workspace, the temporary directory, and the cache directories package managers expect to own.

An explicit `deny` entry wins over any allow, which is the ordinary ACL convention and the one thing in the surface a reader should be able to rely on without checking the implementation.

## The configuration

```yaml
sandbox:
  enforce: required              # required | preferred | off
  filesystem:
    readable: ["~/.config", "~/.ssh", "~/.gitconfig", "~/.cargo", "~/.npmrc", "~/.local"]
    writable: ["$WORKSPACE", "$TMPDIR", "$XDG_CACHE_HOME"]
    deny:     ["~/Documents", "~/Desktop", "~/Downloads", "~/Pictures", "~/Library/Mail"]
  network: allow                 # allow | deny
  limits:
    RLIMIT_CPU: 300
    RLIMIT_AS: 4294967296
    RLIMIT_FSIZE: 1073741824
    RLIMIT_NPROC: 512
    RLIMIT_CORE: 0
  umask: "0077"
  nice: 5
```

`enforce: required` is the default and refuses to create a session when no backend is available, naming what is missing. The alternative was to run unconfined with a warning, and the reason not to is sitting at the top of this document: a configuration key that claims to confine and does not is worse than one that refuses. `preferred` exists for anyone who needs the old behaviour knowingly, and `off` for anyone who does not want it.

## The changes

| # | Change | Where | Why |
|---|---|---|---|
| 1 | `base/confinement.py`: the `Profile`, its resolution, its clamp, and the spawn recipe it produces | new, in `base` | Its two callers are in different layers (`runtime`, `computer`); `base` is the only place both can reach |
| 2 | The POSIX part in a `preexec` hook: rlimits by name, `umask`, `nice`, and a built environment | `base/confinement.py` | Everything but filesystem and network needs no platform code |
| 3 | macOS backend: generated SBPL, executed through `sandbox-exec -p`, marked deprecated where it is defined | `base/confinement.py` | The only per-command mechanism the platform has. Last-match-wins ordering means denials are emitted last |
| 4 | Linux backend: Landlock through `ctypes`, and `unshare(CLONE_NEWUSER\|CLONE_NEWNET)` for network denial | `base/confinement.py` | Landlock's path-beneath rules are the configuration's own shape; bubblewrap would mean rebuilding a filesystem view instead of restricting one, and needs an external binary |
| 5 | Capability probe at daemon start; `enforce: required` refuses session creation and says what is missing | `daemon/__main__.py`, `daemon/api.py` | Failing open quietly is how the current key came to mean nothing |
| 6 | Replace `SandboxConfiguration.enabled: bool` with the surface above | `base/configuration.py:208` | A boolean cannot say what is confined |
| 7 | Per-agent override, alongside the per-agent bash rules | `base/configuration.py` | An investigator and a build agent want different filesystems; agents already differ in what they may do |
| 8 | Resolve and clamp at `session.create` — paths intersect, network takes the stricter, limits the lower | `daemon/api.py:124` | Without the clamp the boundary is one `create_session` deep |
| 9 | `SessionRecord` carries the resolved profile; the worker assignment carries it | `daemon/registry.py`, `daemon/pool.py`, `worker/__main__.py` | The worker applies it to every child, so it must travel with the session |
| 10 | Confine `bash`; replace the `cd <dir> &&` prefix with a real `cwd=` and an absolute workspace in the profile | `runtime/tools/registry.py:102`, `dispatch.py:556` | The working directory is currently shell text inside the model's own command, which can `cd` out of it in the same string |
| 11 | Confine the `control_screen` child with a derived, stricter profile: no network, writes only to the temporary directory | `computer/control.py:59` | Everything it needs is bridged to the parent; nothing it does requires either |
| 12 | Move the bash log off hardcoded `/tmp` onto the confined temporary directory | `runtime/tools/registry.py:85` | Under confinement that path has to be in the writable set or the tool breaks on its own logging |
| 13 | Re-scope the path scan to reads the sandbox permits, and rename it for what it is | `runtime/tools/dispatch.py:324` | It stops being the mechanism and becomes a prompt-injection tripwire |
| 14 | Replace `sandbox_enabled` in the settings DTO and route | `protocol/dtos.py:137`, `rest/routes/settings.py` | The desktop app never rendered a toggle, so this is API-only |
| 15 | Session `public()` gains the resolved profile; regenerate the event schema | `daemon/registry.py`, `web/src/lib/generated/` | `check:events` diffs them; a protocol change without a regenerate fails |
| 16 | Rewrite `configuration.md:85` and `SECURITY.md:21`; add the model to `architecture.md` | `documentation/`, `SECURITY.md` | Both currently assert enforcement that does not exist |
| 17 | Carry the mechanism table above into `SECURITY.md`, and name the `sandbox-exec` dependency and its deprecation in `configuration.md` | `SECURITY.md`, `documentation/configuration.md` | Someone discovering the dependency should find the alternatives and their costs in the same place, not conclude it was chosen carelessly |

## What is deliberately not changing

The permission classifier stays, demoted in the documentation from mechanism to signal. It is a good prompt-injection tripwire and a useful second opinion on a command's intent; it was only ever wrong as the thing standing between a session and the filesystem.

Terminals opened from the desktop app are not confined. They are the user's own shell, opened deliberately by a person, and confining them would be confining the user rather than the agent.

Commands run against a remote location are not confined. `policy.is_remote` executes on another machine through `locations/executor.py`, and a boundary this process draws has no meaning there. The documentation says so rather than leaving it to be discovered.

## Left open

These are known gaps, listed because a design that pretends to have none is the failure this document opens with.

**MCP stdio servers run unconfined.** They are subprocesses the worker spawns, they run with the user's privileges, and they are not covered here. Confining them with the session's profile is a small change and a large breakage risk: many legitimately need network and broad filesystem access, and they are installed deliberately by the user the way a tool is. Damaging that working external architecture to close a hole the user opened knowingly is the wrong order of operations. The question worth answering later is whether an MCP server should carry its own declared profile, the way it already carries its own transport and statefulness.

**Credentials stay readable, so exfiltration is not closed.** `~/.ssh` and its neighbours are in the default allowlist because the tools that need them must keep working. That means a session that is compromised, or merely careless, can still read a private key and send it somewhere the network policy allows. Read confinement here buys protection of personal data, not of secrets. Closing it properly means something other than filesystem ACLs — an agent that can `git push` without holding the key, through an ssh-agent socket or a credential helper that signs rather than reveals — and that is a different piece of work with its own design.

**`sandbox-exec` is deprecated and this depends on it.** The alternatives and why each was rejected are recorded above; the short version is that nothing else on macOS confines a single child without either taking privileges the harness does not have or taking away the user's own files, which is what the harness is for. The plan is to depend on it, say so where it is defined and in the shipped documentation, and replace it if and when Apple ships something per-process. Verifying it still works on the target machine — `sandbox-exec -p '(version 1)(allow default)' /bin/echo ok` — is the first step of implementing this, because everything in row 3 rests on it and none of it can be tested from a Linux host.

**`enforce: required` makes the harness refuse to run where no backend exists.** On Linux that means a kernel without Landlock — older than 5.13 — cannot create sessions at all until the operator sets `preferred`. That is the intended behaviour and it is still a sharp edge, and the refusal message has to be good enough that the fix is obvious from it alone.
