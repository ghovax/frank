# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

- Use GitHub's **[Report a vulnerability](https://github.com/ghovax/daisy/security/advisories/new)**
  (Security → Advisories) to open a private advisory, **or**
- email the maintainer at the address on the [GitHub profile](https://github.com/ghovax).

Include what you found, how to reproduce it, and the impact you expect. You will get an
acknowledgement, and a fix or mitigation will be coordinated with you before any public
disclosure.

## Scope and trust model

Daisy runs AI agents that can execute shell commands, read and write files, control the
Mac through the accessibility API, and drive a browser. **Treat it as software that acts
on your behalf with your privileges.**

- The harness server binds to `127.0.0.1:8822` by default. If you deploy it on a remote
  host, put it behind your own authentication and transport security — it does not add
  its own. Do not expose it directly to the public internet.
- The permission system (approval prompts, sandboxed bash, permission modes) is a
  guardrail against mistakes and prompt-injection, not a sandbox against a determined
  local attacker. Run untrusted tasks accordingly.

## Never commit credentials

API keys and other secrets belong in `~/.daisy/configuration.yaml` (outside the repo) or
in environment variables — never in a tracked file. `~/.daisy/` is gitignored for this
reason, and `configuration.example.yaml` ships with empty values only.

If you believe a key has been exposed, **rotate it at the provider** immediately; removing
it from git history does not un-leak a key that was already pushed.
