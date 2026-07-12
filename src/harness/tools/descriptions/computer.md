**Control this Mac the way a person does.** Drive any native app the user has open by looking at what is on screen and acting on real controls — click a button, type in a field, pick a menu item, scroll, read what a window says. This is how you do things that have no API and no CLI. Everything runs on the user's own machine, and every action is aimed at one specific app, so it never disturbs what the user is doing elsewhere, and their work in other apps never disturbs it. For anything on the **web**, use the `browser` tool instead — it reads a page's real DOM cleanly, without a browser's window chrome.

## How to work: scan, then focus

Operate the computer the way a person would — glance at the whole thing, then look closely at the part that matters. Reach for the approaches in this order:

1. **Navigate the UI yourself — the default.** `observe` an app to get its overview, drill into the region you care about, act on a control by its index (`click`, `type`), and drive it with `key`, `menu`, and `scroll`. This look-act-look loop is real computer use and should handle almost everything.
2. **`screenshot` — when you need to see it.** If `observe` returns little (a canvas-drawn or game UI) or you need the visual layout to decide what to do, capture the window and read the pixels, then act by what you see.
3. **`run_script` — the last resort.** Scripting an app (AppleScript/JXA) is powerful but bypasses real navigation and is brittle across apps and macOS versions. Use it only when driving the UI genuinely cannot do the job — e.g. reading a large structured list an app will not surface in its accessible UI.

## The core loop — observe shallow, then drill

A plain `observe` returns the app's **overview**, not every element: the controls and text near the surface, and any deep container as a single **region** carrying a `children` count. To look inside a region, `observe` again with its `index` as `element` — that expands just that region, again shallow-first. So you scan the app, then focus where the work is, instead of drowning in one giant dump.

Each element carries its real accessibility attributes plus its **`actions`** — the things it can actually do (e.g. `AXPress`). Use them: an element with `AXPress` is clickable; a `heading` or static text with no actions is a label and clicking it does nothing. A field's long text is length-bounded and marked `truncated` when clipped, so if you need the rest, read it another way.

Plan the next few steps from one observe and issue them together in the same turn — a full model turn between every click is what makes this slow. Re-`observe` only when an action **changes what is on screen** (opens a menu or dialog, navigates, adds or removes items), because those shift the indices; read `changes_since_last_observe` to confirm the effect. Acting presses the element in place: no mouse travels the screen and focus is not taken from the user.

## Parameters

| Parameter | Type | Used by | What it is for |
|---|---|---|---|
| `action` | enum | every call | What to do: `observe`, `click`, `type`, `key`, `menu`, `scroll`, `screenshot`, `launch`, `run_script`. |
| `app` | string | observe, key, menu, scroll, screenshot, launch, run_script | The target app's name or bundle id (`Notes`, `com.apple.Finder`). Omit to reuse the last-observed app. |
| `element` | integer | observe (drill), click, type | The `index` of an element from your last `observe` — a region to drill into (observe), or the control to act on (click/type). |
| `text` | string | type, run_script | The text to enter (type), or the script source (run_script). |
| `key` | string | key | One named key: `return`, `tab`, `escape`, `space`, `delete`, arrows (`up`/`down`/`left`/`right`), `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`. |
| `modifiers` | array | key | Modifier keys held with `key`, e.g. `["command"]` or `["command","shift"]`. |
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

- **`observe`** — the thing you do most. Set `app` (and optionally `window`) for the overview, or set `element` to a region's index to drill into it. Start at the overview, drill toward your target, and come back here after every change.
- **`click`** — set `element` to an index from the last observe. Prefer elements whose `actions` include a press action. Use `clicks: 2` to double-click and `button: "right"` for a context menu. If the element had no AX action the click is positional and unconfirmed — the result says so; observe again to check it worked.
- **`type`** — set `element` to a field's index and `text` to what to enter; it replaces the field's contents. Types any script — Latin, CJK, Arabic, emoji.
- **`key`** — set `key` to a named key and optional `modifiers`. For navigation and editing keys (`return` to submit, `escape` to dismiss, arrows to move). For an app command such as copy or select-all, use `menu` — it is the reliable, layout-independent way.
- **`menu`** — set `menu_path` to the menu-bar path as a list. The item is pressed through the menu without a visible flicker.
- **`scroll`** — set `direction` to bring off-screen content into view, then `observe` again to pick up what appeared.
- **`screenshot`** — set `app`; captures its window for you to read visually. Use when the UI has no accessible structure.
- **`launch`** — set `app` to open or bring an app to the front (the reliable way to raise a background Electron app so it builds its full accessible UI). `launch` opens an **app**, not a URL.
- **`run_script`** — last resort. Set `text` to the source and `language`. JXA/AppleScript run in the **automation** context, not a web page — there is no page `window` or DOM. To work with web pages, use the `browser` tool.

## Avoid

- **Reading a web page through this tool.** Use the `browser` tool for the web; it reads the page's own DOM without the tab strip, toolbar, and duplicated markup that reading a browser through the accessibility tree drags in.
- **Reaching for `run_script` first.** Navigate the UI instead; scripting is the last resort, after screenshots.
- **Clicking an element with no press action.** Check `actions`: a `heading` or static text is a label, not a control. Clicking it is positional and usually does nothing.
- **Trusting a stale `index`.** Indices are valid only until your next `observe`; batching several actions from one observe is fine, but once an action opens or closes a view or changes a list, re-observe before using an index.
- **Re-observing after every keystroke.** Observe again after a step that changes the screen's structure, not after routine actions that leave it unchanged; over-observing is the main thing that makes this slow.
