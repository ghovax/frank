**Read and drive the web in the user's own Chrome.** This connects to the user's real, running browser — their profile, their logins, their live session — through Playwright over the DevTools protocol. It reuses the user they are already signed in as (their Gmail, their accounts), never opens a separate browser or copies anything, and never moves the user's cursor. It reads a page's real semantic structure — including the contents of iframes, even cross-origin ones — and acts inside the page with full actionability checks (an element is verified visible, stable, and actually hit by the pointer before a click lands). Use this for anything on the web; use the `computer` tool for native macOS apps.

## The actions

Acting calls return the page's **complete actionable surface**: every interactive element (plus any alert or status announcement), the current `url` and `title`, and honest change flags — everything needed to keep acting, with nothing actionable ever hidden. What they defer is the reading material: the page's prose comes from `read` (or the full tree from `observe`) when you actually want to read, not after every step. Modern sites are JS apps that render late and rewrite the URL as state changes; the tool absorbs both (it waits for content and reports `url_changed`), so trust its feedback rather than re-checking.

- **`navigate`** opens a URL in the user's browser, waits for its content to actually render, and returns the full overview of the new page.
- **`find`** searches the *whole* page (iframes included, past every cap and budget) for elements whose name or value contains `query`, clickable matches first, each registered with an `index` you can act on immediately. Prefer it whenever you can name what you're after. **Match on the role too**: to press a button, pick the `button`/`link` match, not the plain text that merely mentions the same words.
- **`observe`** gives the full lay of the land — elements in page order with `index`, `role`, `name`, `value`, and state flags (`checked`, `disabled`, `expanded`, `selected`). With an `element` index, it expands just that element's subtree in full detail — the tree-shaped way to inspect one card, section, or panel of a large page without paying for the rest (indices then refer to the subtree until the next observe or find). Very large pages are capped and marked `truncated`, and text-only elements past a budget are omitted with a note; `find` and `read` reach everything regardless. A `count: 0` result carries a `hint` — follow it instead of re-observing in a loop.
- **Acting by `index`**: `click`, `type`, `hover`, `select` (a dropdown option by its label), `upload` (attach local files), or `drag` (onto `to_element`). The result lists every interactive element of the resulting page — keep acting directly from it; `observe`/`read` only when you need the text too. `changed: false` with a `note` means nothing happened: adapt rather than clicking again. `type` with `submit: true` presses Enter after filling, when filling and submitting belong together.
- **`scroll`** is a real wheel gesture, exactly like a person: the pointer moves over the target and the wheel turns. With an `element` index, the pointer sits on that element, so the pane *it lives in* scrolls — how you load more of a virtualized list, sidebar, or feed (any element inside the pane works as the target). Without one, the wheel lands at the viewport centre — fine for ordinary pages, but on an app-shell layout (results list beside a map or canvas) the centre may be the map, so target the pane via an `element` instead. `top`/`bottom` fling to the ends. The result carries `changed` (did the page's *content* change — a feed rendering more items, app state updating) and `url_changed`. The overview always covers the whole page regardless of scroll position, so scrolling a static article reveals nothing new — `read` and `find` already see all of it; scroll is for content that loads as you go.
- **`read`** returns visible text one window at a time — the cheapest way to take in an article or long document. With an `element` index it reads only that element's subtree (the article without the page chrome). A truncated result names the exact `offset` that continues it, so long pages are read progressively, never lost.
- **`press`** sends a key or chord to the focused element — `Enter`, `Escape`, `ArrowDown`, `PageDown`, or combinations like `Control+A`. **`back`**/**`forward`**/**`reload`** work as expected.
- **`screenshot`** captures the visible viewport as pixels — the fallback for surfaces with no semantic tree at all (a canvas map, WebGL, a drawing app). Reach for it when `observe` comes back empty with a hint, not before: structured reads are faster and more accurate.

## Dialogs and downloads are handled for you

A JavaScript dialog (`alert`, `confirm`, `prompt`) would freeze the page, so it is answered immediately — alerts acknowledged, questions declined — and reported in the next result as `dialog: {type, message, accepted}`. If a declined `confirm` blocked what you wanted, say so to the user rather than retrying in a loop. A file download triggered by an action is saved and reported as `download: {path}`.

## Watch `url_changed`

Every acting result (`click`, `scroll`, `press`, `type` with submit) carries the current `url` and a `url_changed` flag. Single-page apps encode state — the selected item, the map viewport, the active filter — in the URL, so `url_changed: true` after an action you thought was local means the app state moved (for example, a map pan silently refreshed "results in this area"). Notice it and re-orient instead of carrying on with stale assumptions.

## Tabs

The browser can hold several tabs at once. **`tabs`** lists them, each with a `tab` id, its `title` and `url`, and which one is `active`. **`new_tab`** opens a fresh tab (optionally at a `url`) and makes it active — prefer this when starting new work, so the user's current tab is left alone. **`switch_tab`** makes a given `tab` active; **`close_tab`** closes it. All page actions act on the active tab, so switch first when you mean to work in another one.

## Logins are already there

Because this drives the user's own browser, whatever they are signed into is available — no profile picking, no sign-in step. Just `navigate` to the logged-in site (e.g. `https://mail.google.com`) and it loads as them.

## Browser tool vs. the artifacts panel

This tool *does things* on the real web — checking mail, using an account, filling a form, clicking through a signed-in site — in the user's own Chrome. That is the default for any real web task. The **artifacts panel** (the `open_artifact` tool) is a separate side surface for *previewing* something to look at (a page, a chart, an image). When the user says "open it on the side" or "show it as an artifact", they mean that panel, not Chrome. When they say "check", "log in", "do *this* on the site", or name a real account, they mean this tool. If a request genuinely could go either way — view-only preview versus real interaction — ask which they want rather than guessing.

## Parameters

| Parameter | Type | Used by | What it is for |
|---|---|---|---|
| `action` | enum | every call | `navigate`, `observe`, `find`, `read`, `click`, `type`, `press`, `hover`, `scroll`, `select`, `upload`, `drag`, `screenshot`, `back`, `forward`, `reload`, `tabs`, `new_tab`, `switch_tab`, or `close_tab`. |
| `url` | string | navigate, new_tab | The address to open. |
| `element` | integer | click, type, hover, scroll, select, upload, drag, observe, read | The `index` of an element from the last `observe`/`find`. On `scroll`: an element inside the pane to page through. On `observe`: the subtree to expand. On `read`: the element whose text to read. |
| `text` | string | type | The text to enter into the element. |
| `query` | string | find | The text to search the page for — matched against element names and values, case-insensitively, iframes included. |
| `submit` | boolean | type | Press Enter after typing and return the resulting page. |
| `key` | string | press | The key or chord to press — e.g. `Enter`, `Escape`, `ArrowDown`, `PageDown`, `Control+A`. |
| `direction` | string | scroll | `down` (default), `up`, `left`, `right`, `top`, or `bottom`. |
| `option` | string | select | The visible label (or value) of the option to choose. |
| `paths` | string[] | upload | Local file path(s) to attach. |
| `to_element` | integer | drag | The `index` of the element to drop onto. |
| `offset` | integer | read | Character offset to continue a truncated read from; the previous result names the exact value. |
| `tab` | string | switch_tab, close_tab | The `tab` id from the `tabs` action. |
| `browser_name` | string | navigate, new_tab | Which browser to connect to — `chrome` (default), `edge`, or `brave`. |
| `justification` | string | every call | One plain sentence on why this step is needed. |
| `risk` | enum | every call | `low` for reads/navigation; higher for actions that change something. |

## Avoid

- **Reaching for the `computer` tool to read a web page.** Reading a browser through the macOS accessibility tree drags in the whole window chrome and duplicated markup; this tool reads the page's own structure, cleanly. Web goes here; native apps go to `computer`.
- **Scanning the overview for something you can name.** `find` searches the whole page in one call — iframes and beyond-the-cap content included — and hands back actionable indices.
- **Trusting a stale `index`.** Indices refer to the last `observe`/`find`; after the page changes, get fresh ones before acting.
- **Clicking the text instead of the control.** `find` can match plain text that merely describes the button next to it — act on the match whose role is `button`/`link`/`menuitem` (flagged `clickable`), not the prose.
- **Ignoring the scroll report.** `changed: false` means the page's content is exactly what you already see — the end of a feed, a static page (where `find`/`read` already reach everything), or a wheel that landed on something unscrollable. Repeating the same scroll is never the answer; target a pane by passing an `element` inside it, or switch to `find`/`read`. And a scroll that flips `url_changed` to `true` changed app state (a map pan, an SPA route), not just the view.
- **Re-observing an empty page in a loop.** `count: 0` comes with a `hint` — the page is either still rendering (observe once more, briefly) or canvas-drawn (use `screenshot` or `read`).
- **Hijacking the user's tab for new work.** When a task starts fresh, prefer `new_tab` so you don't navigate away from the page the user was on.
- **Giving up if it says remote debugging is off or stale.** The error explains the one-time switch the user turns on (chrome://inspect) — or, when the endpoint has gone stale, that the switch needs toggling off and on. Relay it plainly and stop, rather than retrying.
