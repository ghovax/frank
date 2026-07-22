# Security Policy

Daisy is software that acts on your behalf with your privileges, so its security depends as much on how you run it as on the code. This policy covers how to report a vulnerability, the trust model you accept when you run Daisy, what it sends to your model provider, and how to keep credentials out of the repository.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

- Use GitHub's **[Report a vulnerability](https://github.com/ghovax/daisy/security/advisories/new)** (Security → Advisories) to open a private advisory, **or**
- email the maintainer at the address on the [GitHub profile](https://github.com/ghovax).

Include what you found, how to reproduce it, and the impact you expect. You will get an acknowledgement, and a fix or mitigation will be coordinated with you before public disclosure.

## Scope and trust model

Daisy runs AI agents that can execute shell commands, read and write files, control the Mac through the accessibility API, and drive a browser. **Treat it as software that acts on your behalf with your privileges.**

- The harness server binds to `127.0.0.1:8822` by default. It adds no authentication or transport security of its own. If you deploy it on a remote host, put it behind your own, and never expose it directly to the public internet.
- The permission system (approval prompts, sandboxed bash, permission modes) is a guardrail against mistakes and prompt-injection, not a sandbox against a determined local attacker. Run untrusted tasks accordingly.

## What the agent sends to your model provider

To be useful from the first turn, Daisy injects two context snapshots into the system prompt: a **system snapshot** (OS, toolchain, `PATH`, environment) and, if you opt in, a **user snapshot** (Git identity, locale, time zone, frequent directories and files, installed and most-used applications, default browser, most-visited sites, and similar signals about how you work). The whole prompt goes to whichever model provider you configured, so **these snapshots put personally identifying information in front of that provider.** Choose your provider with that in mind, and weigh the user snapshot in particular.

This is deliberate on my part as the maintainer, and I implement it knowingly. The goal is to let the agent know who you are, what you work on, and what it can do for you, so it fits your world instead of relearning the basics every turn. It is not settled forever: I am open to a narrower snapshot, redacting or dropping individual fields, or moving more of it behind opt-in — open an issue to shape that. Today the user snapshot is already opt-in, both snapshots are built from local metadata only, and Daisy sends them to your model, not to me or anyone else.

## For contributors

### Never commit credentials

API keys and other secrets belong in `~/.daisy/configuration.yaml` (outside the repo) or in environment variables — never in a tracked file. `~/.daisy/` is gitignored for this reason, and `configuration.example.yaml` ships with empty values only.

If a key has been exposed, **rotate it at the provider** immediately. Removing it from git history does not un-leak a key that was already pushed.
