---
created: 2026-07-26T15:22:03Z
updated: 2026-07-26T15:22:03Z
commit: 39e581a
---

# Back to Daisy

The harness was renamed from Daisy to XEAC, and the name is being taken back. There is no engineering argument in either direction — the owner prefers Daisy — so this plan is not about whether, only about doing it once, completely, and without leaving the old name in the places that are easy to miss: an environment variable, a wire namespace, a generated TypeScript type, a directory under `$XDG_CONFIG_HOME`.

The repository was never renamed. It is still `ghovax/daisy`, which means the seven GitHub URLs across `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `documentation/installation.md` and `protocol/card.py` — the ones that have been quietly wrong since the migration, one of them served on every agent card — become correct with no action at all.

## What the name is attached to

One thousand two hundred and twenty-one occurrences outside the plans directory, in four distinct casings, and the casings do not map onto one another cleanly. `XEAC_` prefixes environment variables and one protocol constant, and must become `DAISY_`; the bare `XEAC` is a display name and must become `Daisy`; lowercase `xeac` is the command, the package, the socket paths and the bundle identifier, and becomes `daisy`; the single mixed-case `Xeac` appears only inside a generated TypeScript namespace. The substitution therefore has to run in a fixed order — `XEAC_` before `XEAC`, or `XEAC_API_BASE` becomes `Daisy_API_BASE` — and that ordering is the one mechanical thing in this plan that can silently go wrong.

Beyond the text, five things carry the name as an identifier rather than as a word, and each fails differently if it is missed. `APPLICATION` in `base/paths.py` is a single constant that names every XDG directory and every socket path the harness opens; changing it moves configuration, state, transcripts, caches and the runtime directory in one line. The Tauri bundle identifier `com.ghovax.xeac` is what macOS attributes the Accessibility grant to. The three `urn:xeac:*` extension URIs are advertised on the agent card and read out of turn metadata, which makes them the only part of this rename that crosses a machine boundary. The generated `XeacEvents` namespace is produced by `scripts/generate_event_schema.py` and must be regenerated rather than edited, or the next regeneration silently reverts it. And two paragraphs of prose expand the acronym, which a substitution cannot fix at all.

## The two paragraphs a substitution would ruin

`README.md` and `runtime/prompts/system_prompt.md` both explain that XEAC stands for X, Executable, Addressable, Composable. Substituting the product name into those sentences produces *"Daisy is what the harness makes of whatever you substitute in"*, which is not a sentence about anything. They have to be rewritten by hand.

The system-prompt one matters more than its length suggests. It is where a session is told what it is — one OS process with a pid, reachable on its own socket — and that framing is load-bearing for the paragraph after it, which explains why a peer answers by sending a message rather than by returning a value. Deleting the acronym must not delete the properties. The replacement states them directly:

> You run as one OS process with a pid, reachable on your own socket, holding your own capability token. That is why a peer is not a subroutine: it is a session like you, with its own process and its own address, and it answers by sending you a message rather than by returning a value.

The README paragraph loses the wordplay about `xargs` and `xdg-open`, which was only ever a justification for a letter that is no longer there, and keeps the three properties in a shorter form.

## What is deliberately not renamed

`documentation/plans/` holds eighty-six occurrences and none of them change. The plans are dated records of work as it was done, and one of them is the record of the migration this plan reverses; rewriting them to say `daisy` would make them describe a history that did not happen. A plan is evidence, not documentation, and evidence that is edited to match the present is worth nothing. The same reasoning does not apply to `.agents/memories/`, which are live operational notes rather than records, and are renamed.

The word *server* is also left alone here, even though it is the other piece of naming debt in the tree, because the desktop app is about to stop bundling the daemon at all and that change decides which of those names survive. Renaming them now would mean renaming them twice. For the same reason `packaging/xeac-server.spec` is renamed only mechanically, to `daisy-server.spec`, rather than to the name it should eventually have.

## Costs accepted

Changing the bundle identifier orphans the macOS Accessibility grant, and every user grants it once more. Changing `APPLICATION` means `~/.config/xeac/` and `~/.local/share/xeac/history.db` are no longer read: Daisy starts with a freshly seeded configuration and an empty transcript store. Neither gets a migration path — there is no backward-compatibility requirement, and a detect-and-move step is exactly the kind of code that outlives its reason. `installation.md` gains one sentence saying where the old files are, for anyone who wants to move them by hand. `~/.agents/` is unbranded and carries over untouched, so agent profiles, skills, memories and MCP servers survive the rename without doing anything.

Renaming the `urn:xeac:*` namespaces breaks the wire contract with any peer running an un-renamed build. That is accepted rather than worked around: remote peers are the owner's own instances, and leaving `xeac` inside the extension URIs of a product called Daisy would not remove the problem, only relocate it somewhere harder to find.

## Order of work

The substitution runs first and in the fixed order above, then the package directory moves, then the three identifier sites that a text pass cannot reach — the XDG application name, the generated events namespace by way of its generator, and the wire URIs. The two prose paragraphs are written last, by hand, because they are the only part of this that requires judgement.

Verification is mechanical and should be treated as the point of the exercise rather than a formality: `ruff`, the layering check, an import sweep over every module, the tuning attribute check, the confinement and pool and end-to-end suites, `cargo check`, `bun run lint`, a first-run configuration seed into the new XDG directory, `daisy configure --all` returning its hundred and four settings, and a final grep for each of the four casings returning zero outside the plans directory.
