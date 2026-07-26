# Security Policy

Daisy is software that acts on your behalf with your privileges, so its security depends as much on how you run it as on the code. This policy covers how to report a vulnerability, the trust model you accept when you run Daisy, what it sends to your model provider, and how to keep credentials out of the repository.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

- Use GitHub's **[Report a vulnerability](https://github.com/ghovax/daisy/security/advisories/new)** (Security → Advisories) to open a private advisory, **or**
- email the maintainer at the address on the [GitHub profile](https://github.com/ghovax).

Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement, and a fix or mitigation will be coordinated with you before public disclosure.

## Scope and trust model

Daisy runs AI agents that can execute shell commands, read and write files, control the Mac through the accessibility API, and drive a browser. **Treat it as software that acts on your behalf with your privileges.**

- `daisyd` listens on a unix socket in your runtime directory and on an ephemeral loopback port, both gated by a capability token it writes `0600`. Each session has its own socket and its own token, minted at creation. That is access control between local users, not transport security: the token crosses the wire in the clear, so if you reach a daemon from another machine, tunnel it over SSH or put TLS in front, and never expose the port directly to the public internet.
- A session's permission mode is fixed at creation and a child is clamped to no looser a mode than its parent. There is no bypass mode and no standing "always allow".
- That clamp depends on the daemon knowing which session made a call, and a token cannot establish it: a session runs as you, so it can read the daemon's own `0600` token as easily as any other file of yours. On the unix socket the daemon therefore takes the caller's identity from the kernel — `SO_PEERCRED` on Linux, `LOCAL_PEERPID` on macOS — and resolves the pid to a session through the process session each worker leads, which covers the worker and everything it shells out to. That identification wins over whatever token was presented, so holding the daemon's token buys a session no anonymity. A caller that `setsid`s itself is placed in no session, but it has also left the process group `daisy kill` signals and the tree the reaper walks.
- A session's tool children — shell commands and screen-control scripts — are confined by the operating system: `sandbox-exec` with a generated Seatbelt profile on macOS, Landlock plus a network namespace on Linux. Your home directory is unreadable to them except for an allowlist, writes are narrower still, and the confinement is fixed when the session is created and clamped against its creator. It is configured in `sandbox:` — see the [Configuration guide](documentation/configuration.md#confinement).
- **`sandbox-exec` is deprecated by Apple** and Daisy depends on it, because nothing else on macOS confines one child process: App Sandbox applies to a whole signed application and would confine the harness out of the files it exists to reach, Endpoint Security observes rather than bounds, and a separate uid, `chroot` or a container stops the agent being able to act as you. If it stops working, the boot-time probe fails and `sandbox.enforce` decides whether sessions refuse to start or run unconfined.
- **Credentials are readable, so this does not stop exfiltration.** `~/.ssh` and its neighbours are in the default allowlist because the tools that need them must keep working; a session that is compromised can still read a key and send it where the network policy allows. What the filesystem rules protect is your personal data, not your secrets.
- **MCP servers are not confined.** They are subprocesses you installed deliberately, they run with your privileges, and many need network and broad filesystem access to do their job.
- The permission system (approval prompts, permission modes, the command classifier) is a guardrail against mistakes and prompt-injection layered *over* that boundary, not the boundary itself. Run untrusted tasks accordingly.

## What the agent sends to your model provider

To be useful from the first turn, Daisy injects two context snapshots into the system prompt: a **system snapshot** (OS, toolchain, `PATH`, environment) and, if you opt in, a **user snapshot** (Git identity, locale, time zone, frequent directories and files, installed and most-used applications, default browser, most-visited sites, and similar signals about how you work). The whole prompt goes to whichever model provider you configured, so **these snapshots put personally identifying information in front of that provider.** Choose your provider with that in mind, and weigh the user snapshot in particular.

This is deliberate on my part as the maintainer, and I implement it knowingly. The goal is to let the agent know who you are, what you work on, and what it can do for you, so it fits your world instead of relearning the basics every turn. It is not settled forever: I am open to a narrower snapshot, redacting or dropping individual fields, or moving more of it behind opt-in — open an issue to shape that. Today the user snapshot is already opt-in, both snapshots are built from local metadata only, and Daisy sends them to your model, not to me or anyone else.

## For contributors

### Never commit credentials

API keys and other secrets belong in `~/.config/daisy/configuration.yaml` (outside the repo) or in environment variables — never in a tracked file. `~/.config/daisy/` lives outside the repository for this reason, and the packaged template the harness seeds it from carries empty values only.

If a key has been exposed, **rotate it at the provider** immediately. Removing it from git history does not un-leak a key that was already pushed.
