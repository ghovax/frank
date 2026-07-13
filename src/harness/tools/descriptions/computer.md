**Control this Mac the way a person does.** Drive any native app the user has open by looking at what is on screen and acting on real controls — click a button, type in a field, pick a menu item, scroll, read what a window says. This is how you do things that have no API and no CLI. Everything runs on the user's own machine, and every action is aimed at one specific app, so it never disturbs what the user is doing elsewhere, and their work in other apps never disturbs it. For anything on the **web**, use the `browser` tool instead — it reads a page's real DOM cleanly, without a browser's window chrome.

## How to work: scan, then focus

Operate the computer the way a person would — glance at the whole thing, then look closely at the part that matters. Reach for the approaches in this order:

1. **Navigate the UI yourself — the default.** `observe` an app to get its overview, drill into the region you care about (or jump straight to a control by text with `find`), act on a control by its index (`click`, `type`), and drive it with `press`, `menu`, and `scroll`. This look-act-look loop is real computer use and should handle almost everything.
2. **`screenshot` — when you need to see it.** If `observe` returns little (a canvas-drawn or game UI) or you need the visual layout to decide what to do, capture the window and read the pixels, then act by what you see.
3. **`run_script` — the last resort.** Scripting an app (AppleScript/JXA) is powerful but bypasses real navigation and is brittle across apps and macOS versions. Use it only when driving the UI genuinely cannot do the job — e.g. reading a large structured list an app will not surface in its accessible UI.

## The core loop — observe shallow, then drill

A plain `observe` returns the app's **overview**, not every element: the controls and text near the surface, and any deep container as a single **region** carrying a `children` count. To look inside a region, `observe` again with its `index` as `element` — that expands just that region, again shallow-first. So you scan the app, then focus where the work is, instead of drowning in one giant dump.

Each element carries a `role`, its accessible `name`, its `value`, and — for controls — `clickable: true` and the list of AX `actions` it supports (e.g. `AXPress`). Use them: a `clickable` element with an `AXPress` action is a control; a `heading` or static text with no actions is a label and clicking it does nothing. A `region` carries a `children` count instead of contents — drill into it. Long text is length-bounded and marked `truncated` when clipped, so if you need the rest, read it another way.

Every acting call (`click`, `type`, `press`, `menu`, `scroll`) re-reads the app for you and returns its **actionable surface** — the current set of clickable elements, with fresh indices — plus a `changed` flag saying whether anything moved. So you do not need to `observe` again just to see the effect of an action; act on the returned indices directly. `observe` when you need the full tree or the prose, or to drill into a region. Plan the next few steps from one surface and issue them together in the same turn — a full model turn between every click is what makes this slow. Acting presses the element in place: no mouse travels the screen and focus is not taken from the user.

## Parameters

| Parameter | Type | Used by | What it is for |
|---|---|---|---|
| `action` | enum | every call | What to do: `observe`, `find`, `click`, `type`, `press`, `menu`, `scroll`, `screenshot`, `launch`, `run_script`. |
| `app` | string | observe, find, press, menu, scroll, screenshot, launch, run_script | The target app's name or bundle id (`Notes`, `com.apple.Finder`). Omit to reuse the last-observed app. |
| `element` | integer | observe (drill), click, type | The `index` of an element from your last `observe`/`find` — a region to drill into (observe), or the control to act on (click/type). |
| `text` | string | type, run_script | The text to enter (type), or the script source (run_script). |
| `query` | string | find | Text to search the app's on-screen elements for — matches names and values, case-insensitively. |
| `key` | string | press | One named key: `return`, `tab`, `escape`, `space`, `delete`, arrows (`up`/`down`/`left`/`right`), `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`, or a chord with `modifiers`. |
| `modifiers` | array | press | Modifier keys held with `key`, e.g. `["command"]` or `["command","shift"]`. |
| `menu_path` | array | menu | The menu-bar path as a list of titles, e.g. `["Edit","Select All"]`. |
| `direction` | string | scroll | `up`, `down`, `left`, or `right`. |
| `arguments` | array | launch | Launch arguments for the app, e.g. `["--profile-directory=Profile 2"]`. |
| `language` | string | run_script | `applescript` (default) or `javascript` (JXA). |
| `window` | string | observe | Scope: `focused` (default), `main`, or `all`. |
| `clicks` | integer | click | `2` for a double-click (default `1`). |
| `button` | string | click | `left` (default) or `right` for a context-menu click. |
| `justification` | string | every call | One plain sentence on why this step is needed. |
| `risk` | enum | every call | `low` for reads (observe, screenshot); higher for actions that change something. |

## Actions in detail

- **`observe`** — the thing you do most. Set `app` (and optionally `window`) for the overview, or set `element` to a region's index to drill into it. Start at the overview, drill toward your target. After an action you rarely need it — the action already returns the fresh surface.
- **`find`** — jump straight to a control by its text. Set `query` (and optionally `app`); it searches the app's on-screen elements and returns the matches, clickable ones first, each with an index you can act on. Faster than drilling when you know what the control says.
- **`click`** — set `element` to an index from the last observe/find. Prefer `clickable` elements whose `actions` include a press action. Use `clicks: 2` to double-click and `button: "right"` for a context menu. If the element had no AX action the click is positional and unconfirmed — the result says so and returns the resulting surface so you can check the `changed` flag.
- **`type`** — set `element` to a field's index and `text` to what to enter; it replaces the field's contents. Types any script — Latin, CJK, Arabic, emoji.
- **`press`** — set `key` to a named key and optional `modifiers`. For navigation and editing keys (`return` to submit, `escape` to dismiss, arrows to move) and chords. For an app command such as copy or select-all, prefer `menu` — it is the reliable, layout-independent way.
- **`menu`** — set `menu_path` to the menu-bar path as a list. The item is pressed through the menu without a visible flicker.
- **`scroll`** — set `direction` to bring off-screen content into view, then `observe` again to pick up what appeared.
- **`screenshot`** — set `app`; captures its window for you to read visually. Use when the UI has no accessible structure.
- **`launch`** — set `app` to open or bring an app to the front (the reliable way to raise a background Electron app so it builds its full accessible UI). `launch` opens an **app**, not a URL.
- **`run_script`** — last resort. Set `text` to the source and `language`. JXA/AppleScript run in the **automation** context, not a web page — there is no page `window` or DOM. To work with web pages, use the `browser` tool.

## Avoid

- **Reading a web page through this tool.** Use the `browser` tool for the web; it reads the page's own DOM without the tab strip, toolbar, and duplicated markup that reading a browser through the accessibility tree drags in.
- **Reaching for `run_script` first.** Navigate the UI instead; scripting is the last resort, after screenshots.
- **Clicking an element with no press action.** Check `actions`: a `heading` or static text is a label, not a control. Clicking it is positional and usually does nothing.
- **Trusting a stale `index`.** Indices are valid only until your next `observe`, `find`, or acting call — each returns a fresh surface with fresh indices. Batching several actions from one surface is fine, but do not reuse an index from before the surface you are now looking at.
- **Re-observing after every action.** Every acting call already returns the app's fresh actionable surface and a `changed` flag — act on those indices or read `changed` instead of issuing a separate `observe`. Observe again only for the full tree, the prose, or to drill a region; over-observing is the main thing that makes this slow.
