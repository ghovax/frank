**Read and drive the web in the user's own Chrome.** This connects to the user's real, running browser over the DevTools protocol and drives it directly — their profile, their logins, their live session. It reuses the user they are already signed in as (their Gmail, their accounts), never opens a separate browser or copies anything, and never moves the user's cursor. It reads a page's real semantic structure (clean roles and names straight from the DOM, with none of the tab strip, toolbar, or duplicated markup you get from reading a browser through the accessibility tree) and acts inside the page itself. Use this for anything on the web; use the `computer` tool for native macOS apps.

## How to work: navigate, then read and act

Everything a person does in a browser is here — navigate, read, click, type, press keys, hover, scroll, go back/forward, reload, and manage tabs.

1. **`navigate`** to a URL. This opens the page in the user's browser and returns its overview right away.
2. **`observe`** to read the current page again after it changes — it returns the page's elements shallow-first, each with an `index`, a semantic `role` (`link`, `button`, `textbox`, `heading`, …), its `name`, and its `value`. Deep or large containers (a long list, an inbox grid) come back as **regions** carrying a `children` count; pass that region's index as `element` to `observe` to drill in and expand just that region. Re-observe after navigating or acting, since indices change.
3. **Act** by an element's `index`: `click` it, `type` into it (the text is pasted in one shot, firing the page's real input events), or `hover` it to reveal a menu or tooltip. The role tells you what an element is for — a `link` or `button` is clickable, a `textbox`/`searchbox`/`combobox` is editable, a `heading` is just a label. A `click` result reports `changed`: `true` means the page moved (or the element toggled in place), `false` with a `note` means nothing happened — adapt (a different, `clickable` target, or navigate directly by URL) rather than clicking again.
4. **`press`** a key on the focused element — most often `Enter` to submit a search or form after you `type`, but also `Escape`, `Tab`, the arrows, `PageDown`, and so on. Click or type into a field first so it holds focus.
5. **`scroll`** the page — `down` (default) or `up` by most of a viewport, `top`/`bottom` to the ends, or pass an `element` index to bring that element into view.
6. **`read`** returns the page's visible text in one block when you just need to read, not navigate. **`back`**/**`forward`** move through history; **`reload`** refreshes the page.

## Tabs

The browser can hold several tabs at once. **`tabs`** lists them, each with a `tab` id, its `title` and `url`, and which one is `active`. **`new_tab`** opens a fresh tab (optionally at a `url`) and makes it active — prefer this when starting new work, so the user's current tab is left alone. **`switch_tab`** makes a given `tab` active; **`close_tab`** closes it. All page actions (`observe`, `click`, `read`, …) act on the active tab, so switch first when you mean to work in another one.

## Logins are already there

Because this drives the user's own browser, whatever they are signed into is available — no profile picking, no sign-in step. Just `navigate` to the logged-in site (e.g. `https://mail.google.com`) and it loads as them.

## Parameters

| Parameter | Type | Used by | What it is for |
|---|---|---|---|
| `action` | enum | every call | `navigate`, `observe`, `read`, `click`, `type`, `press`, `hover`, `scroll`, `back`, `forward`, `reload`, `tabs`, `new_tab`, `switch_tab`, or `close_tab`. |
| `url` | string | navigate, new_tab | The address to open. |
| `element` | integer | click, type, hover, observe, scroll | The `index` of an element from the last `observe` (on `observe`, a region to drill into; on `scroll`, the element to bring into view). |
| `text` | string | type | The text to enter into the element. |
| `key` | string | press | The key to press — e.g. `Enter`, `Escape`, `Tab`, `ArrowDown`, `PageDown`. |
| `direction` | string | scroll | `down` (default), `up`, `top`, or `bottom`. |
| `tab` | string | switch_tab, close_tab | The `tab` id from the `tabs` action. |
| `browser_name` | string | navigate, new_tab | Which browser to connect to — `chrome` (default), `edge`, or `brave`. |
| `justification` | string | every call | One plain sentence on why this step is needed. |
| `risk` | enum | every call | `low` for reads/navigation; higher for actions that change something. |

## Avoid

- **Reaching for the `computer` tool to read a web page.** Reading a browser through the macOS accessibility tree drags in the whole window chrome and duplicated markup; this tool reads the page's own DOM, cleanly. Web goes here; native apps go to `computer`.
- **Trusting a stale `index`.** Indices refer to the last `observe`; after you navigate or act, `observe` again before using an index.
- **Clicking a non-interactive element.** Let the `role` guide you — a `heading` or `StaticText` is a label, not a control; click `link`/`button`/`menuitem` and edit `textbox`/`searchbox`.
- **Expecting `type` to submit.** It only fills the field. To run a search or submit a form, `press` `Enter` afterward (or click the submit button) — and click/type the field first so it holds focus for the key.
- **Hijacking the user's tab for new work.** When a task starts fresh, prefer `new_tab` so you don't navigate away from the page the user was on.
- **Giving up if it says remote debugging is off.** The error explains the one-time switch the user turns on (chrome://inspect); relay it plainly and stop, rather than retrying.
