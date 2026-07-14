**Control this Mac the way a person does.** Drive any native app the user has open by looking at what is on screen and acting on real controls — click a button, type in a field, pick a menu item, scroll, read what a window says. This is how you do things that have no API and no CLI. Everything runs on the user's own machine, and every action is aimed at one specific app, so it never disturbs what the user is doing elsewhere, and their work in other apps never disturbs it. For anything on the **web**, use the `browser` tool instead — it reads a page's real DOM cleanly, without a browser's window chrome.

## How to work: scan, then focus

Operate the computer the way a person would — glance at the whole thing, then look closely at the part that matters. Reach for the approaches in this order:

1. **Navigate the UI yourself — the default.** `observe` an app to get its overview, drill into the region you care about (or jump straight to a control by text with `find`), act on a control by its `ref` (`click`, `type`), edit text precisely (`select`, `caret`, `edit`, `copy`/`cut`/`paste`), and drive it with `press`, `menu`, and `scroll`. This look-act-look loop is real computer use and should handle almost everything.
2. **`screenshot` — when you need to see it.** If `observe` returns little (a canvas-drawn or game UI) or you need the visual layout to decide what to do, capture the window and read the pixels, then act by what you see.
3. **Scripting a cooperative app — use the `bash` tool, not this one.** When an app exposes a real scripting model (Calendar, Mail, Notes, Music, Finder) and you want its structured data rather than to drive its UI, run `osascript` through the `bash` tool on this same Mac — e.g. `osascript -e '…'` (AppleScript) or `osascript -l JavaScript -e '…'` (JXA). That is the accurate, fast path for an app's data; there is no scripting action here.

## The core loop — observe shallow, then drill

A plain `observe` returns the app's **overview**, not every element: the controls and text near the surface, and any deep container as a single **region** carrying a `children` count. To look inside a region, `observe` again with its `index` as `element` — that expands just that region, again shallow-first. So you scan the app, then focus where the work is, instead of drowning in one giant dump.

Each element carries a stable `ref` — an opaque handle like `k7Bn2p` — a `role`, its accessible `name`, its `value`, and — for controls — `clickable: true` and the list of AX `actions` it supports (e.g. `AXPress`). Use them: a `clickable` element with an `AXPress` action is a control; a `heading` or static text with no actions is a label and clicking it does nothing. A `region` carries a `children` count instead of contents — drill into it. You act by `ref`, and a ref is stable: it names the same element across calls, so an `observe` in between seeing an element and acting on it does not invalidate the refs you hold, and a `find` that matches nothing never wipes them.

Structural actions (`click`, `press`, `menu`, `scroll`, `hover`, `drag`, and a whole-field `type`) re-read the app and return a **diff** of what changed — elements that `appeared`, `disappeared`, or `updated`, plus a `changed` flag — not the whole surface again; only a wholesale change (a new window, a near-total redraw) returns the full fresh surface. So you do not need to `observe` again just to see the effect; read the diff and act on the refs you already have. `observe` when you need the full tree or the prose, or to drill into a region. Plan the next few steps and issue them together in the same turn — a full model turn between every click is what makes this slow. Acting presses the element in place: no mouse travels the screen and focus is not taken from the user.

## Editing text — by content, not by counting keystrokes

Inside a text field you work at the level of the text itself, addressing positions and ranges by **what the text says**, never by nudging the caret one arrow-key at a time:

- **`select`** picks a range: a substring (`text`), a span from one phrase through another (`text` + `to_text`), or the whole field (`select_all`). The selection is what formatting and replacement act on — for example, select a phrase and `press` the app's bold shortcut, or select and `type` to overtype it.
- **`caret`** places the insertion point: `before`/`after` a phrase, `at_offset` a character offset, or at the `start`/`end` edge.
- **`edit`** changes part of a field by replacing exact `find` text with `replace` text — the same find-must-be-unique matching as the file-edit tool, returning a small before/after diff, not the whole field. Prefer it over rewriting a whole field to change a few words.
- **`type`** enters text: `mode:"replace"` (the default) rewrites the field in one shot — good for filling an empty field; `mode:"insert"` inserts at the caret, replacing any selection.
- **`copy`/`cut`/`paste`** use the real system clipboard, so text moves between apps and between here and the `browser` tool.

These text actions return only what they changed (the selection, the caret, or a diff), and they do **not** re-read the whole app, so refs stay stable — chain several on one field in a turn. A whole-field `type` reads the field back and returns its resulting `value`, so a length limit that silently clamped your text is visible. When a field is a custom or canvas-drawn editor with no accessible caret, these say so; screenshot it and act by clicking at a position instead.

## Parameters

| Parameter | Type | Used by | What it is for |
|---|---|---|---|
| `action` | enum | every call | What to do: `observe`, `find`, `click`, `type`, `edit`, `select`, `caret`, `copy`, `cut`, `paste`, `press`, `menu`, `scroll`, `hover`, `drag`, `screenshot`. |
| `app` | string | observe, find, press, menu, scroll, screenshot | The target app's name or bundle id (`Notes`, `com.apple.Finder`). Omit to reuse the last-observed app. |
| `element` | string | observe (drill), click, type, edit, select, caret, copy, cut, paste, hover, drag | The `ref` of an element from an earlier `observe`/`find` — a region to drill into (observe), the control to act on, or the field to edit. Refs survive across calls. |
| `text` | string | type, select | The text to enter (type), or the substring to select (select). |
| `query` | string | find | Text to search the app's on-screen elements for — matches names and values, case-insensitively. |
| `find` / `replace` | string | edit | The exact existing text to change, and what to change it to. |
| `replace_all` | bool | edit | Change every occurrence of `find` rather than requiring it to be unique. |
| `mode` | string | type | `replace` (rewrite the whole field, default) or `insert` (at the caret, replacing any selection). |
| `to_text` | string | select | The end anchor for a range — select from `text` through `to_text`. |
| `select_all` | bool | select | Select the whole field. |
| `occurrence` | integer | select, caret | Which occurrence to target when the text appears more than once (1-based, default `1`). |
| `before` / `after` | string | caret | Place the caret just before / just after this text. |
| `at_offset` | integer | caret | Place the caret at this character offset. |
| `edge` | string | caret | Place the caret at the field `start` or `end`. |
| `key` | string | press | One named key: `return`, `tab`, `escape`, `space`, `delete`, arrows (`up`/`down`/`left`/`right`), `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`, or a chord with `modifiers`. |
| `modifiers` | array | press | Modifier keys held with `key`, e.g. `["command"]` or `["command","shift"]`. |
| `menu_path` | array | menu | The menu-bar path as a list of titles, e.g. `["Edit","Select All"]`. |
| `direction` | string | scroll | `up`, `down`, `left`, or `right`. |
| `x` / `y` | integer | click, hover, drag | A screen point to act at when there is no element to target (the pixel fallback). |
| `to_element` | string | drag | The `ref` of the element to drop onto. |
| `to_x` / `to_y` | integer | drag | The screen point to drag to. |
| `window` | string | observe | Scope: `focused` (default), `main`, or `all`. |
| `clicks` | integer | click | How many times to click — `1` (default, activates/selects), `2` to open a folder/file/row (double-click), `3` to select a line (triple-click). |
| `button` | string | click, drag | `left` (default) or `right` for a context-menu click. |
| `justification` | string | every call | One plain sentence on why this step is needed. |
| `risk` | enum | every call | `low` for reads (observe, screenshot); higher for actions that change something. |

## Actions in detail

- **`observe`** — the thing you do most. Set `app` (and optionally `window`) for the overview, or set `element` to a region's ref to drill into it. Start at the overview, drill toward your target. After an action you rarely need it — the action already returns the diff.
- **`find`** — jump straight to a control by its text. Set `query` (and optionally `app`); it searches the app's on-screen elements and returns the matches, clickable ones first, each with a `ref` you can act on. Faster than drilling when you know what the control says.
- **`click`** — set `element` to a ref from an earlier observe/find, or `x`/`y` for a raw point when there is no element (a canvas). A single click **activates or selects** (it fires the element's `AXPress` action — a button, link, or checkbox — else it just selects, like a Finder cell). To **open** a folder, file, or list row, use `clicks: 2` — a double click fires the element's `AXOpen` action, which is how macOS opens things; a single click on such an item only selects it (the result hints at this). `clicks: 3` triple-clicks (selecting a line in text), and `button: "right"` opens a context menu. If no AX action matched, the click is a positional synthesized click, unconfirmed — the result says so and returns the diff so you can check the `changed` flag.
- **`type`** — set `element` to a field's ref and `text` to what to enter. `mode:"replace"` (default) rewrites the whole field and reads it back so a clamped value is visible; `mode:"insert"` inserts at the caret. Types any script — Latin, CJK, Arabic, emoji.
- **`edit`** — change part of a field: set `element`, `find` (the exact current text), and `replace`. `find` must be unique unless you set `replace_all`. Returns a small before/after diff. This is the precise way to change a few words without rewriting everything.
- **`select`** — select text in a field to format or overtype it: a substring (`text`), a range (`text` + `to_text`), or the `select_all` field. Use `occurrence` when the text repeats.
- **`caret`** — place the insertion point in a field: `before`/`after` a phrase, `at_offset` a character offset, or the `start`/`end` edge. Then `type` with `mode:"insert"` to add text there.
- **`copy` / `cut` / `paste`** — the current selection to and from the real system clipboard. Copy/cut need a selection first; paste drops the clipboard at the caret.
- **`press`** — set `key` to a named key and optional `modifiers`. For navigation and editing keys (`return` to submit, `escape` to dismiss, arrows to move) and chords — including formatting shortcuts (e.g. bold) applied to the current selection. For an app command such as select-all, `menu` is also reliable and layout-independent.
- **`menu`** — set `menu_path` to the menu-bar path as a list. The item is pressed through the menu without a visible flicker.
- **`hover`** — move the pointer over an `element` or `x`/`y` point to reveal a hover menu or tooltip, without clicking.
- **`drag`** — press at a source (`element` or `x`/`y`) and release at a target (`to_element` or `to_x`/`to_y`) — for drag and drop or dragging to select.
- **`scroll`** — set `direction` to bring off-screen content into view; the result's diff shows what appeared.
- **`screenshot`** — set `app`; captures its window for you to read visually. Use when the UI has no accessible structure.
This tool drives apps that are **already open**. It does not launch or script them — those are plain shell commands, so run them through the `bash` tool on this same Mac:

- **To open (or raise) an app** — `open -a "Slack"`. This is also how you wake a background Electron app so it builds its full accessible tree before you `observe`.
- **To script a cooperative app** for its structured data (AppleScript/JXA) — `osascript -e '…'` or `osascript -l JavaScript -e '…'`. It runs in the automation context, not a web page (no `window`/DOM); for web pages use the `browser` tool.

## Avoid

- **Reading a web page through this tool.** Use the `browser` tool for the web; it reads the page's own DOM without the tab strip, toolbar, and duplicated markup that reading a browser through the accessibility tree drags in.
- **Looking for a scripting action here.** There isn't one — to script an app (AppleScript/JXA), run `osascript` through the `bash` tool. Drive the UI with this tool; script for structured data with bash.
- **Clicking an element with no press action.** Check `actions`: a `heading` or static text is a label, not a control. Clicking it is positional and usually does nothing.
- **Re-observing to "refresh" a ref.** Refs are stable — they name the same element across calls, so act on the one you already have. A `find` that matches nothing does not wipe them, and an action returns a diff, not a renumbered surface. Re-observe for the full tree, the prose, or to drill a region — not to keep a ref alive.
- **Re-observing after every action.** Every acting call already returns a diff of what changed and a `changed` flag — read that instead of issuing a separate `observe`. Over-observing is the main thing that makes this slow.
