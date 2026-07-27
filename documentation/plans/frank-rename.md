---
created: 2026-07-27T21:20:00Z
updated: 2026-07-27T21:20:00Z
commit: b912526
---

# Daisy becomes Frank

There is no engineering argument here. The owner wants the harness to have a person's name of the same shape as the assistant it is built around — we have Claude, so Frank — and that is the whole reason. So this plan is not about whether. It is about doing it once, completely, and not leaving the old name in the places that are easy to miss: a wire namespace, a bundle identifier, a generated TypeScript type, a directory under `$XDG_CONFIG_HOME`.

This is the second rename this tree has had. The first, [`daisy-rename.md`](./daisy-rename.md), went the other way — XEAC back to Daisy — and its record is worth reading before starting, because most of what went wrong there was mechanical and is avoidable here by knowing about it.

## What is different this time

**The repository name stops being free and starts being a cost.** Last time the repository was already `ghovax/daisy` while the code said `xeac`, so eight GitHub URLs that had been quietly wrong became correct with no action. This time it inverts: those same eight URLs — in `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `documentation/installation.md` and `protocol/card.py`, the last of which is served on every agent card — are correct today and become wrong the moment the product is called Frank. Either the repository is renamed to `ghovax/frank` (GitHub redirects the old path, so nothing breaks immediately) or the URLs stay pointing at a repository whose name no longer matches the product. **This is the one decision in this plan that cannot be made inside the repository**, and everything else assumes the rename happens.

**There is no acronym to unpick.** XEAC expanded to X, Executable, Addressable, Composable, and two paragraphs of prose were built on that expansion, which a substitution turned into nonsense. Daisy expands to nothing and no prose plays on the word — a grep for daisy-chaining and for "the name Daisy" returns nothing. So the only prose that needs a human is the single line of `runtime/prompts/system_prompt.md:11` that names the harness to the model, and the README's opening, and both are a straight swap rather than a rewrite.

**The tree is roughly twice as branded.** 1,144 lowercase `daisy`, 320 `Daisy`, 99 `daisyd`, 34 `DAISY`. The growth is mostly the library surface and its documentation, which did not exist last time.

## What the name is attached to

Four casings, and unlike XEAC they map onto one another cleanly — `DAISY_`→`FRANK_`, `Daisy`→`Frank`, `daisy`→`frank` — so a naive ordered substitution is safe. `daisyd` needs no rule of its own: it falls out of `daisy`→`frank` as `frankd`, which is what it should be.

Beyond the text, nine things carry the name as an **identifier** rather than as a word. Each fails differently if it is missed, and none of them is caught by the test suite.

| Identifier | Where | What it costs to miss |
|---|---|---|
| `APPLICATION = "daisy"` | `base/paths.py:20` | Names every XDG directory and socket path in one constant. Miss it and the product is Frank while its files live under `daisy/` |
| `com.ghovax.daisy` | `cli/__main__.py:314`, `web/src-tauri/tauri.conf.json:5`, both packaging scripts | The bundle identifier macOS attributes the Accessibility grant to. Must be identical across the daemon bundle and the app, or they become two TCC rows |
| `urn:daisy:ext:turn:v1` | `protocol/metadata.py:14` | Advertised on the agent card and read out of every turn's metadata. **The only part of this rename that crosses a machine boundary** |
| `urn:daisy:ext:content-block:v1` | `base/message_content.py:14` | Same, on assistant content blocks |
| `urn:daisy:a2a:file:v1` | `protocol/files.py:146` | A signed token's audience. Changing it invalidates every outstanding file link, which is correct — an audience that names the wrong product is the bug |
| `DaisyEvents` | `scripts/generate_event_schema.py:145` | **Generated**, not written. Editing `web/src/lib/generated/events.ts` by hand is reverted by the next `bun run build:events`, and `check:events` fails |
| `ORIGINATOR = "daisy"` / `USER_AGENT` | `base/subscription.py:46,52` | What the ChatGPT endpoint is told this client is. Free to change — opencode sends its own name, which is the proof no exact impersonation is required |
| `PACKAGE = "daisy"` | `scripts/check_layers.py:57` | The layering checker resolves nothing and passes vacuously if this does not match the package directory |
| `default-run = "Daisy"` | `web/src-tauri/Cargo.toml:7` | The binary's name is capitalised deliberately: the macOS Apple-Events prompt shows the *executable* name, and a lowercase one would not match the "Frank" the Accessibility pane shows |

## Files and directories that move

| From | To | Note |
|---|---|---|
| `src/daisy/` | `src/frank/` | `git mv`, so history follows |
| `packaging/daisy-daemon.spec` | `packaging/frank-daemon.spec` | Also referenced by `build-daemon.sh` |
| `web/src-tauri/Daisy.icon` | `web/src-tauri/Frank.icon` | Referenced from `tauri.conf.json` |
| `documentation/assets/daisy-social-card.png` | `frank-social-card.png` | Referenced from `README.md` |
| `Daisy Computer Use.app` | `Frank Computer Use.app` | The bundle name in `daisy-daemon.spec`'s `BUNDLE(...)` step |
| `daisyd.sock`, `daisyd.lock` | `frankd.sock`, `frankd.lock` | Fall out of the text pass; listed because the smoke test in `build-daemon.sh:95` hard-codes the path |

Package names change too: `pyproject.toml`'s `name = "daisy"` and its `[project.scripts]` entry, and `web/package.json`'s `daisy-web`. **Check that `frank` is available on PyPI before committing to it**; the project is not published today, so this is a decision about the future rather than a blocker.

## What is deliberately not renamed

`documentation/plans/` is left alone entirely, including `daisy-rename.md`'s filename. The plans are dated records of work as it was done, and one of them is the record of the rename this one supersedes; rewriting them to say `frank` would make them describe a history that did not happen. **A plan is evidence, not documentation, and evidence edited to match the present is worth nothing.** This file is the exception that proves it: it is the record of *this* rename, so it is written in both names on purpose.

`.agents/` is unbranded and carries over untouched — agent profiles, skills, memories and MCP servers survive without doing anything. The three `.agents/memories/*.md` that mention Daisy are live operational notes rather than records, so they are renamed.

`Claude` stays wherever it appears as a model name, an attribution, or a co-author trailer. The point of the new name is that the harness has a name of the same *kind* as the model it drives, not that it takes it over.

## Costs accepted

**The Accessibility grant is orphaned.** Changing the bundle identifier means macOS sees new code identity, and every user grants screen control once more. There is no migration and there should not be — TCC is keyed on identity precisely so that a change of identity is a change of subject.

**Configuration, transcripts and state are left behind.** `~/.config/daisy/`, `~/.local/share/daisy/history.db`, `~/.local/state/daisy/` and the runtime directory are no longer read; Frank starts with a freshly seeded configuration and an empty transcript store. No detect-and-move step: there is no backward-compatibility requirement, and migration code outlives its reason by years. `installation.md` gains one sentence saying where the old files are, for anyone who wants to move them by hand.

**The wire contract breaks with any peer running an un-renamed build.** The three `urn:daisy:*` namespaces are what a remote peer reads turn metadata and content blocks out of. Accepted rather than worked around: remote peers are the owner's own instances, and leaving `daisy` inside the extension URIs of a product called Frank would relocate the problem rather than remove it.

**Outstanding signed file links stop working**, because the token audience changes. That is a minute's inconvenience and the correct behaviour: an audience exists so a token minted for one purpose is not accepted for another.

## Order of work

Each step is a commit, and the order is chosen so that nothing is ever half-renamed in a way the checks cannot see.

1. **Move the package.** `git mv src/daisy src/frank`, then `PACKAGE = "frank"` in the layering checker, then `pyproject.toml`. Nothing imports yet; that is expected and is why this is first rather than interleaved.
2. **The text pass**, over tracked files only, excluding `documentation/plans/`: `DAISY_`→`FRANK_`, `DAISY`→`FRANK`, `Daisy`→`Frank`, `daisy`→`frank`, in that order. `.venv/`, `node_modules/` and `.git/` are excluded by scoping to `git ls-files`, not by a filter that can be got wrong.
3. **The identifier sites** a text pass reaches but should be reviewed one at a time — the nine in the table above — because each is a decision rather than a substitution.
4. **Regenerate**, never edit: `bun run build:events` for the events namespace, and `cargo check` for the Rust side.
5. **The two prose sites**, by hand: `system_prompt.md:11` and the README's opening.
6. **The moved files**, with `git mv`, and their referrers.

## Verification

Mechanical, and the point of the exercise rather than a formality.

| Check | Proves |
|---|---|
| `git grep -in daisy -- ':!documentation/plans'` returns **zero** | The rename is complete where it should be |
| `git grep -c daisy -- documentation/plans` is **unchanged** | The records were not rewritten |
| `uv run ruff check src/ scripts/` | Nothing was left syntactically broken |
| `uv run python scripts/check_imports.py` | Every module imports under its new name |
| `uv run python scripts/check_layers.py` | The checker resolves the *new* package, and did not pass vacuously |
| `uv run python -m scripts.verify` — all 13 stages | The harness works, not merely imports. **The battery is the real check**: it starts a daemon, forks a session through the prototype, drives a turn, sleeps and wakes it, and asserts the XDG directories it uses |
| `cd web && bun run build` | `check:events`, `check:translations` and the type check, including the regenerated `FrankEvents` |
| `cargo check --manifest-path web/src-tauri/Cargo.toml` | The Tauri shell still builds under its new binary name |
| A first run seeds `~/.config/frank/configuration.yaml` | `APPLICATION` actually moved |
| `frank configure --all` lists every setting | The command exists under its new name and reads the new directory |
| On macOS: the two bundles show as **one** "Frank" row in Accessibility | The identifier changed in all four places, not three |

## Hazard register

| Hazard | Why it is real | Detection |
|---|---|---|
| **The generated events file is edited instead of regenerated** | It is a normal-looking TypeScript file that a text pass will happily rewrite, and the result survives until someone runs the generator | `bun run check:events` diffs generated output against the committed file and fails |
| **The bundle identifier is changed in three places out of four** | It appears in the CLI, the Tauri config and both packaging scripts, and a partial change produces *two* TCC rows rather than an error | The macOS check in the verification table; nothing catches it on Linux |
| **The layering checker passes vacuously** | `PACKAGE` not matching the directory means it resolves no imports and reports success | It is checked as part of step 1, before anything depends on it |
| **`documentation/plans/` gets swept up** | It is the natural target of a repository-wide substitution, and the damage is invisible — the plans still read fine, they are just no longer true | The unchanged-count check, which is why it is phrased as a count rather than an eyeball |
| **A stale `src/daisy.egg-info` or `__pycache__` shadows the new package** | An editable install resolves the old name from metadata, so imports keep working locally and fail everywhere else | `check_imports.py` in a clean checkout; delete both before verifying |
| **PyPI already has `frank`** | The name is plausible enough to be taken | Check before committing to it; the project is unpublished, so this is a future cost rather than a blocker |
