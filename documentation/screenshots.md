# Capturing the README screenshots

The README references five images under `documentation/assets/screenshots/`. This guide is how to (re)capture them so they stay consistent.

## Before you start

- **Collapse the Recents sidebar** for every shot (it shows real session titles). Use the collapse toggle next to the conversation title, or work inside a fresh, empty project.
- **Hide personal data.** No real API keys (they should show as dots — never capture a visible key), no personal file paths, no private session titles.
- **Set a normal permission mode.** For the permissions shot you need `default` (not `bypass`), so the approval overlay actually appears. Switch it back afterward if you like.

## How to capture one window cleanly

`⌘⇧4`, then press **Space**, then click the **Daisy window**. macOS saves a clean PNG (with the window shadow) to the Desktop. Rename it to the target filename and move it into `documentation/assets/screenshots/`.

## The shot list

| File | Screen | How to stage it |
|------|--------|-----------------|
| `hero.png` | Chat with live tool calls | New conversation, sidebar collapsed. Send a neutral task that uses a couple of tools, then capture once the tool cards and the answer are visible. |
| `providers.png` | Model providers | Open **Settings → Providers**. Make sure keys read as dots, not plaintext. Capture the settings window. |
| `computer-use.png` | Computer-use / browser | Run a task that uses the browser or computer tool. Capture with the `browser` (or `computer`) tool card visible. |
| `artifacts.png` | Artifacts & projects | Produce an artifact, then open the **Artifacts** view. Capture with the rendered artifact shown. |
| `permissions.png` | Permission approval | With permission mode `default`, run a task whose tool call needs approval. Capture **while the approval overlay is showing**, before you decide. |

## After capturing

Drop all five files into `documentation/assets/screenshots/` with exactly the names above, then commit.
