# Capturing the README screenshots

The README and docs reference a fixed set of images under `documentation/assets/screenshots/`. This guide is how to (re)capture them so they stay consistent. Each screen is shot in **both themes** — a dark and a light version — and the README auto-switches between them with `<picture>`.

## Before you start

- **Collapse the Recents sidebar** for every shot (it shows real session titles). Use the collapse toggle next to the conversation title. Or work inside a fresh, empty project.
- **Hide personal data.** No real API keys (they should show as dots — never capture a visible key), no personal file paths, no private session titles.
- **Set a normal permission mode.** For the permissions shot you need `default` (not `bypass`), so the approval overlay actually appears. Switch it back afterward if you like.
- **Theme.** Capture all screens in dark, then switch the app (or macOS appearance) to light and capture them again.

## How to capture one window cleanly

`⌘⇧4`, then press **Space**, then click the **Daisy window**. macOS saves a clean PNG (with the window shadow) to the Desktop. Rename it to the target filename and move it into `documentation/assets/screenshots/`.

## The shot list

| File | Screen | How to stage it |
|------|--------|-----------------|
| `hero-dark.png` / `hero-light.png` | Chat with live tool calls | New conversation, sidebar collapsed. Send a neutral task that uses a couple of tools, e.g. *"Write a small Python script in a scratch folder that prints the current time in Tokyo, London, and New York, then run it and show the output."* Wait until the tool cards and the answer are visible, then capture. |
| `providers-dark.png` / `providers-light.png` | Model providers | Open **Settings → Providers**. Make sure keys read as dots, not plaintext. Capture the settings window. |
| `computer-use-dark.png` / `computer-use-light.png` | Computer-use / browser | Run a task that uses the browser or computer tool, e.g. *"Open example.com in my browser and tell me the page's title."* Capture with the `browser` (or `computer`) tool card visible. |
| `artifacts-dark.png` / `artifacts-light.png` | Artifacts & projects | Produce an artifact, e.g. *"Make a simple bar chart of the values 3, 7, 4, 9 and open it as an artifact,"* then open the **Artifacts** view. Capture with the rendered artifact shown. |
| `permissions-dark.png` / `permissions-light.png` | Permission approval | With permission mode `default`, run a task whose tool call needs approval (e.g. a shell command). Capture **while the approval overlay is showing**, before you decide. |

## After capturing

Drop all ten files into `documentation/assets/screenshots/` with exactly the names above, then commit. If you decide to skip the light variants, say so — the README `<picture>` blocks can be switched to dark-only so nothing renders broken.
