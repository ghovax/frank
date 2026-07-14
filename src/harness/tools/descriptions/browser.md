**Read and drive the web in the user's own Chrome.** This connects to the user's real, running browser — their profile, their logins, their live session — through Playwright over the DevTools protocol. It reuses the user they are already signed in as (their Gmail, their accounts), never opens a separate browser or copies anything, and never moves the user's cursor. It reads a page's real semantic structure — including the contents of iframes, even cross-origin ones — and acts inside the page with full actionability checks (an element is verified visible, stable, and actually hit by the pointer before a click lands). Use this for anything on the web; use the `computer` tool for native macOS apps.

## Elements are addressed by a stable `ref`

Every element an `observe` or `find` returns carries a `ref` — an opaque handle like `k7Bn2p`. You act by that ref — `click`, `type`, `scroll`, and the rest all take an `element` ref. A ref is **stable**: it names the same element across calls, so an `observe` or `find` in between seeing an element and acting on it does **not** invalidate the refs you already hold, and a `find` that matches nothing never wipes what you were working through. A ref whose element has left the page fails cleanly with a "observe again" message — it never silently acts on the wrong thing.

Each element also carries a `context` — the heading, card, or section it sits under — so twenty identical "Add to Cart" buttons are told apart by the product above each one. Read the `context` to pick the right one.

## Results are a diff, not a re-listing

An acting call returns **what changed**, not the whole page again:

- `changed` — whether the page's content moved at all; `url_changed` — whether the URL changed (an SPA route, a real navigation).
- `changes` — the structured delta: elements that `appeared` (a menu opened, a row expanded), `disappeared`, or `updated` (a value or state changed).
- `announcements` — any alert or status the page raised in response (a validation error, "Added to cart").

Only a **wholesale** change — a navigation, a near-total rerender — falls back to returning the full fresh surface, because there a diff would be the whole page and you need the new lay of the land. When you want the complete tree or the prose regardless, call `observe` or `read`. This keeps every step small; trust the diff instead of re-observing.

## The actions

- **`navigate`** opens a URL and returns the full overview. It waits for the DOM to parse and the network to briefly settle — never for one stalled ad or tracker — so a heavy page comes back promptly rather than hanging on a hung resource.
- **`observe`** gives the full lay of the land — every element in page order with its `ref`, `role`, `name`, `value`, `context`, and state flags. With an `element` ref it expands just that element's subtree (the tree-shaped way to inspect one card or panel of a large page). Very large pages are capped and marked `truncated`; pass a **`goal`** (what you're trying to reach) and the cap keeps the relevant controls instead of whatever comes first in the DOM. A `count: 0` result carries a `hint` — follow it instead of re-observing in a loop.
- **`find`** searches the *whole* page (iframes included, past every cap) for elements whose name, value, or context contains `query`, clickable matches first, each with its `context`. Prefer it whenever you can name what you're after. **Match on the role too**: to press a button, pick the `button`/`link` match, not the plain text that mentions the same words.
- **Acting by `ref`**: `click`, `type`, `hover`, `choose` (a dropdown option by label), `upload` (attach files), or `drag` (onto `to_element`, or between points). `click` takes `clicks` (2/3 for double/triple), `button` (`right` for a context menu), and can act at an `x`/`y` viewport point when there is no element (a canvas). Pass **`expect`** (text the action should produce) and the tool waits for it and reports `expected_found`; pass **`dialog`** (`accept`/`dismiss`) to decide a confirm/prompt the click triggers. `changed: false` with a note means nothing happened — adapt rather than clicking again.
- **`type`** enters text and reads the field back, returning its resulting `value` — so a `maxlength` clamp or input mask that silently altered your text is visible, not assumed. `mode:"insert"` inserts at the caret; `submit: true` presses Enter after filling. To change part of a field, prefer `edit`.
- **Editing text by content**: `select` picks a range (`text`, a `text`→`to_text` span, or `select_all`); `caret` places the insertion point (`before`/`after` a phrase, `at_offset`, or the `start`/`end` edge); `edit` replaces exact `find` text with `replace` (verbatim, must be unique unless `replace_all`) and returns a small before/after diff; `type` with `mode:"insert"` types at the caret. `copy`/`cut`/`paste` use the real OS clipboard. These text actions return only what they changed and keep refs stable, so chain several on one field. A canvas-drawn editor (e.g. Google Docs) has no real DOM text to map onto; the action says so — screenshot it and act by position.
- **`scroll`** is a real wheel gesture. With an `element` ref the pointer sits on that element, so the pane *it lives in* scrolls — how you load more of a virtualized list or feed. Without one the wheel lands at the viewport centre. `top`/`bottom` fling to the ends. The result's `changes` show what new content appeared; on an infinite feed, keep scrolling while new items keep appearing.
- **`read`** returns visible text one window at a time — the cheapest way to take in an article. With an `element` ref it reads only that subtree. A truncated result names the exact `offset` to continue from.
- **`evaluate`** runs a JavaScript `expression` in the page and returns its JSON result — the direct path to structured extraction (reading an XHR endpoint, pulling a table into an array, computing an aggregate) instead of paging text windows. It runs in the user's real, signed-in page, so it is as privileged as anything on that origin; use it deliberately.
- **`network`** returns the recent requests the page made (method, url, status, type), newest last, optionally filtered by a url substring in `query`. The data endpoints behind a rendered view are often the fastest, most accurate thing to read.
- **`press`** sends a key or chord to the focused element. **`back`**/**`forward`**/**`reload`** work as expected.
- **`screenshot`** captures the visible viewport as pixels — the fallback for surfaces with no semantic tree (a canvas map, WebGL). The capture is at CSS-pixel scale, so a coordinate you read off the image is the same coordinate a click uses, on Retina displays included. Reach for it when `observe` comes back empty with a hint, not before.

## Dialogs and downloads are handled for you

A JavaScript dialog (`alert`, `confirm`, `prompt`) would freeze the page, so it is answered immediately — an alert acknowledged, a question declined — and reported in the next result as `dialog: {type, message, accepted}`. To go through with a confirm (a delete-with-confirm flow), pass `dialog: "accept"` on the action that triggers it. A file download triggered by an action is saved and reported as `download: {path}`.

## Watch `url_changed`

Single-page apps encode state — the selected item, the map viewport, the active filter — in the URL, so `url_changed: true` after an action you thought was local means the app state moved. Notice it and re-orient instead of carrying on with stale assumptions.

## Tabs

**`tabs`** lists open tabs, each with a `tab` id, `title`, `url`, and which is `active`. **`new_tab`** opens a fresh tab (optionally at a `url`) and makes it active — prefer this for new work so the user's current tab is left alone. **`switch_tab`** activates a tab; **`close_tab`** closes it. All page actions act on the active tab.

## Logins are already there

Because this drives the user's own browser, whatever they are signed into is available — no profile picking, no sign-in step. Just `navigate` to the logged-in site (e.g. `https://mail.google.com`) and it loads as them.

## Browser tool vs. the artifacts panel

This tool *does things* on the real web — checking mail, using an account, filling a form, clicking through a signed-in site — in the user's own Chrome. That is the default for any real web task. The **artifacts panel** (the `open_artifact` tool) is a separate side surface for *previewing* something to look at (a page, a chart, an image). "Open it on the side" / "show it as an artifact" means that panel, not Chrome. "Check", "log in", "do *this* on the site" means this tool. If a request could genuinely go either way, ask which they want.

## Parameters

| Parameter | Type | Used by | What it is for |
|---|---|---|---|
| `action` | enum | every call | `navigate`, `observe`, `find`, `read`, `click`, `type`, `edit`, `select`, `caret`, `copy`, `cut`, `paste`, `press`, `hover`, `scroll`, `choose`, `upload`, `drag`, `evaluate`, `network`, `screenshot`, `back`, `forward`, `reload`, `tabs`, `new_tab`, `switch_tab`, or `close_tab`. |
| `url` | string | navigate, new_tab | The address to open. |
| `element` | string | click, type, edit, select, caret, copy, cut, paste, hover, scroll, choose, upload, drag, observe, read | The `ref` of an element from an earlier `observe`/`find` — the control, the field, the pane to scroll, the subtree to expand, or the element to read. Refs survive across calls. |
| `text` | string | type, select | The text to enter (type), or the substring to select (select). |
| `query` | string | find, network | The text to search the page for (find), or a url substring to filter by (network). |
| `find` / `replace` | string | edit | The exact existing text to change, and what to change it to. |
| `replace_all` | bool | edit | Change every occurrence of `find` rather than requiring it to be unique. |
| `mode` | string | type | `replace` (rewrite the whole field, default) or `insert` (at the caret, replacing any selection). |
| `submit` | boolean | type | Press Enter after typing and return the resulting page. |
| `to_text` | string | select | The end anchor for a range — select from `text` through `to_text`. |
| `select_all` | bool | select | Select the whole field. |
| `occurrence` | integer | select, caret | Which occurrence to target when the text repeats (1-based, default `1`). |
| `before` / `after` | string | caret | Place the caret just before / just after this text. |
| `at_offset` | integer | caret | Place the caret at this character offset. |
| `edge` | string | caret | Place the caret at the field `start` or `end`. |
| `key` | string | press | The key or chord to press — e.g. `Enter`, `Escape`, `ArrowDown`, `PageDown`, `Control+A`. |
| `direction` | string | scroll | `down` (default), `up`, `left`, `right`, `top`, or `bottom`. |
| `option` | string | choose | The visible label (or value) of the dropdown option to pick. |
| `paths` | string[] | upload | Local file path(s) to attach. |
| `goal` | string | observe, navigate | What you're trying to reach; ranks a capped listing so the relevant controls survive. |
| `expect` | string | click, type, press, navigate | Text the action should produce; the tool waits for it and reports `expected_found`. |
| `dialog` | string | click | `accept` or `dismiss` a JavaScript confirm/prompt the action triggers. |
| `expression` | string | evaluate | A JavaScript expression to run in the page; its JSON result is returned. |
| `x` / `y` | integer | click, hover, drag | A viewport point to act at when there is no element (the canvas fallback). |
| `to_element` | string | drag | The `ref` of the element to drop onto. |
| `to_x` / `to_y` | integer | drag | The viewport point to drag to. |
| `clicks` | integer | click | How many times to click — `1` (default), `2` double, `3` triple. |
| `button` | string | click | `left` (default) or `right` for a context-menu click. |
| `offset` | integer | read | Character offset to continue a truncated read from; the previous result names the exact value. |
| `tab` | string | switch_tab, close_tab | The `tab` id from the `tabs` action. |
| `browser_name` | string | navigate, new_tab | Which browser to connect to — `chrome` (default), `edge`, or `brave`. |
| `justification` | string | every call | One plain sentence on why this step is needed. |
| `risk` | enum | every call | `low` for reads/navigation; higher for actions that change something. |

## Avoid

- **Reaching for the `computer` tool to read a web page.** This tool reads the page's own structure cleanly; the accessibility tree drags in the whole window chrome and duplicated markup. Web goes here; native apps go to `computer`.
- **Scanning the overview for something you can name.** `find` searches the whole page in one call — iframes and beyond-the-cap content included — and hands back a `ref` and `context`.
- **Re-observing to "refresh" a ref.** Refs survive across calls; act on the one you have. Re-observe for the full tree or prose, not to keep a ref alive.
- **Clicking the text instead of the control.** `find` can match plain text that merely describes the button next to it — act on the match whose role is `button`/`link`/`menuitem` (flagged `clickable`), not the prose. When several match, read `context` to pick the right one.
- **Ignoring the diff.** `changed: false` means the page's content did not move — the end of a feed, a static page, or a wheel on something unscrollable. Repeating the same action is never the answer; target a pane by passing an `element` inside it, or switch to `find`/`read`/`evaluate`.
- **Paging text windows when the data is structured.** For a table, a list, or an XHR-backed view, `evaluate` a small expression or read `network` — far faster and more accurate than reading 16 KB windows.
- **Re-observing an empty page in a loop.** `count: 0` comes with a `hint` — the page is either still rendering (observe once more) or canvas-drawn (use `screenshot` or `read`).
- **Hijacking the user's tab for new work.** When a task starts fresh, prefer `new_tab`.
- **Giving up if it says remote debugging is off or stale.** The error explains the one-time switch the user turns on (chrome://inspect). Relay it plainly and stop, rather than retrying.
