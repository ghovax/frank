---
created: 2026-07-25T13:13:38Z
updated: 2026-07-25T13:13:38Z
commit: 7e0062f
---

# The Browser Has Tabs and Frames the Model Cannot Name

A session driving the user's Chrome sees exactly one page, chosen for it by a heuristic it cannot observe or override, and reads that page as if it were flat. Neither is true of the browser. The user has a dozen tabs open; the page has an iframe holding the payment form, the OAuth consent, or the embedded document that is the entire point of the task. The harness knows about both — it tracks every tab in a registry nothing reads, and it already addresses elements inside iframes correctly — and it exposes neither. This plan makes both nameable, and closes a permission hole found while looking at them.

The hole comes first because it is live and it is small. The screen tool's permission classifier decides what a script may do by walking its AST for state-changing primitive names, and the set it walks for is `{click, type, choose, upload, drag}`. `evaluate` is not in it. So a script whose only act is running arbitrary JavaScript inside the user's signed-in page is classified `read_only`, which means two things at once: it passes a read-only policy that exists to hard-block every write, and — when its declared `risk` is `low` — it raises no permission gate at all, because the gate is only built when the classification is not read-only or the risk is at least medium. `evaluate` can POST, can DELETE, can read a session token out of the page and send it somewhere; it is the single most consequential primitive the tool has, and it is the one primitive that runs unexamined. `press` is in the same class by the tool's own description, which states that `press("Enter")` posts a form. `navigate` is there too, for a reason worth arguing rather than asserting: on a great many sites a URL is a command, not an address — `/logout`, `/unsubscribe?token=…`, `/items/12/delete` — and a name-based classifier cannot tell those from a page the model wants to read.

## What is already there

`_Session` in `computer/web.py` carries `tab_ids` and `pages_by_id`, a bidirectional registry from Playwright `Page` objects to model-facing identifiers (`tab1`, `tab2`, …). Every page is entered into it by `adopt`, and `adopt` is wired to `context.on("page", …)`, so the registry already covers not just the tabs the harness opens but the ones the *user* opens and the popups a click spawns. Nothing reads it. No primitive returns a tab, accepts a tab, or closes one. The single reference to a second page anywhere in the surface is `navigate(new_tab=True)`, which creates a page, makes it active, and does not tell the caller what it made. Which page a script acts on is decided by `_pick_page`: the first page whose URL starts with `http`, else the last page in the list. That is a reasonable guess and it is entirely invisible — a script cannot ask which tab it is on, cannot move to another, and cannot get back to where it started.

The registry is also never pruned. A `Page` that has been closed stays in both dictionaries for the life of the connection, so anything built on it has to filter for liveness rather than trust it.

Frames are the opposite situation: the hard part is already solved and the easy part is missing. The page is read with Playwright's ai-mode aria snapshot, which inlines iframe contents and prefixes the references inside each frame — an element in the first frame is `f1e3`, in the second `f2e2`, and a frame nested inside the second is `f3`. Those references work: `page.locator("aria-ref=f1e3")` resolves through the iframe boundary to the real element, and it does so for nested frames as well. Every acting primitive therefore already works across frames, and has done since the snapshot was adopted, by accident rather than by design. What does not work is everything that needs to *name* a frame. `evaluate` runs its JavaScript in the main frame only, so the page's own API cannot be called from inside the iframe whose session it belongs to. `read` reads the main frame's body. And there is no way to ask what frames exist at all, so a model looking at an element called `f1e3` has no way to learn what `f1` is, where it came from, or what it is showing.

The mapping needed to fix that is available and cheap. An `iframe` node in the snapshot carries its own reference like any other element, and the nodes indented beneath it carry the frame prefix — so the snapshot itself states which element owns which frame. Resolving that element handle to a live `Frame` is one call, and the frame's parent falls out of the same reading: the owning iframe's own reference carries the prefix of the frame *it* sits in, so the iframe at `e4` belongs to the main frame and the iframe at `f2e3` belongs to `f2`.

## What a reference actually promises

Two properties of aria references matter to this design, and one of them contradicts what the code currently says about them.

They are more stable than advertised. `_locator` documents its argument as "valid until the next snapshot on the page", and that is not what happens: taking a second snapshot of an unchanged page yields byte-identical references, and inserting new content assigns new numbers to the new elements while leaving existing ones exactly where they were. A reference survives re-reading. This matters because `find_one` and `find_many` take a fresh snapshot on every call, so under the pessimistic reading every find would invalidate every reference the script was holding, and scripts would have been written defensively for no reason. The docstring should say what is true.

A reference that no longer resolves does not fail; it waits. Playwright treats `aria-ref=e9` for an element that has left the document the way it treats any other selector that matches nothing — it retries until the timeout, which this surface sets to `ACTION_TIMEOUT_MS`. That is the correct behaviour for an action, where the element may be about to appear, and the wrong shape for an enumeration: `frames()` resolves every iframe reference it found, so a single stale one would cost the full action timeout before the listing came back. It has to pass a short explicit timeout of its own.

## Tabs

Four primitives, all of them thin over the registry that already exists.

`tabs()` returns the live pages as `{id, title, url, active}`, pruning any that have closed as it goes. `tab(id)` makes one active and calls `bring_to_front()`, which on the user's real Chrome raises the window — a visible side effect on the user's own machine, and the right one, since the point of this tool is to act as the user rather than beside them. `new_tab(url="")` creates a page, makes it active, and returns its identifier, which is what `navigate(new_tab=True)` should have done and did not; `navigate` loses its `new_tab` parameter, because opening a tab is not a way of navigating. `close_tab(id="")` closes the given tab or the active one, drops it from the registry, and re-picks an active page through the existing `_pick_page` when it closed the one that was in use.

`_pick_page` stays exactly as it is. It is the right answer to "nothing has been chosen yet"; it was only ever wrong as the answer to "which tab am I on", and that question now has a primitive.

## Frames

`frames()` returns `{id, url, name, parent, element}` for every frame in the page — `id` the same `f1`/`f2` the element references already use, `parent` the frame that owns it (empty for a frame directly in the main document), and `element` the reference of the `iframe` element itself, so a model that wants to scroll the frame into view or read its surroundings can act on the frame as an element too. The identifiers are Playwright's, taken from the snapshot rather than minted here, which is a deliberate coupling: one vocabulary across elements and frames is worth more than independence from a numbering scheme, and inventing a second set of frame names when the element references already carry one would guarantee they drift.

`evaluate` and `read` gain a `frame` argument. Without it they behave as they do today, against the main frame. With it they run against the named frame, which is what makes an embedded checkout, an OAuth consent screen, or a document viewer readable and scriptable at all.

`documents()` adds a `frame` field to the payload of any element that sits inside one, derived from the prefix its own reference already carries. It costs nothing to compute and it answers, at the moment the model first sees `f1e3`, the question it would otherwise have to spend a call on.

## The changes

| # | Change | Where | Why |
|---|---|---|---|
| 1 | `_MUTATING_SCREEN_PRIMITIVES` gains `evaluate`, `press`, `navigate` | `runtime/permissions.py:25` | Arbitrary JavaScript in the user's signed-in page currently classifies read-only, passes a read-only policy, and at `risk: low` raises no gate at all |
| 2 | The same set gains `new_tab` and `close_tab`; `tab`, `tabs` and `frames` stay read-only | `runtime/permissions.py:25` | Opening and closing tabs changes state; switching to one and listing them is reading |
| 3 | `tabs()` — live pages as `{id, title, url, active}`, pruning closed ones | `computer/web.py` | The registry has been built on every `adopt` since it was written and no primitive has ever read it |
| 4 | `tab(id)` — make a tab active and bring it to front | `computer/web.py` | Which page a script acts on is decided by `_pick_page` and cannot be observed or overridden |
| 5 | `new_tab(url="")` → the new tab's id; `navigate` loses `new_tab` | `computer/web.py:759` | Creating a tab is not a kind of navigating, and the caller is never told what it made |
| 6 | `close_tab(id="")` — close, unregister, re-pick the active page if needed | `computer/web.py` | Nothing can be closed today, so a script that opens tabs leaves them on the user's screen |
| 7 | `frames()` → `{id, url, name, parent, element}`, resolved with a short timeout | `computer/web.py` | Elements are already frame-addressed; nothing says what a frame *is* |
| 8 | `_parse_snapshot` records `fK → owning iframe reference`; the frame resolves via that element's handle | `computer/web.py:279` | The snapshot already states the ownership; this reads it instead of guessing an order |
| 9 | `frame=` on `evaluate` and `read` | `computer/web.py:731,742` | Both are main-frame-only, so an embedded checkout or consent screen is unreadable and unscriptable |
| 10 | `documents()` payload gains `frame` for elements inside one | `computer/web.py:509` | Answers what `f1` is at the moment the model first sees `f1e3` |
| 11 | `_PRIMITIVES` gains `tabs`, `tab`, `new_tab`, `close_tab`, `frames`; none of them join `targeting_verbs` | `computer/control_child.py:34`, `runtime/tools/dispatch.py:1322` | The injection allowlist. A tab id is not an element query and must not be resolved as one |
| 12 | Correct `_locator`'s docstring: references survive re-snapshotting; a dead one costs a timeout, not an error | `computer/web.py:493` | It currently promises the opposite of what the snapshot does |
| 13 | Rewrite the tool description and the screen-control section of the docs | `runtime/tools/registry.py:744`, `documentation/tools.md:67` | Both describe a browser with one page and no frames |

## What is deliberately not changing

The native macOS surface gains nothing. It has no tabs and no frames, and the shared `Surface` contract already tolerates web-only primitives — `control_child` binds every name unconditionally and the native surface answers "the computer surface has no such action", which is the honest outcome and the existing precedent set by `evaluate` and `navigate`.

The desktop client needs no changes, which is worth stating because the last four plans all touched it. `controlScreenLabel` summarises a call by its script's first line and never enumerates primitives; `ControlScreenCallView` renders the surface and the script; `ControlScreenResultView` renders `value`, `stdout`, `acted_on` and errors generically. A list of tabs or frames is the script's return value and renders as one without knowing what it is.

`acted_on` is left alone. It records which *element* each mutating action touched, and a tab switch touches no element; adding a different kind of entry to it would blur what it means for the one consumer that reads it.

Nothing here adds a wait, a timeout, or a CDP call. Those are the other two groups and they both touch the permission classifier and the timeout stack; keeping them out means this change can be judged on its own.

## Open questions

**Should `tabs()` list tabs the model did not open?** `context.pages` is every tab in the user's real Chrome, so the honest implementation hands a session the titles and URLs of the user's entire open browsing session — banking, mail, whatever is behind them. Nothing else in the harness exposes that today: `documents()` reads only the active page. The alternatives are to list everything (most useful, and consistent with a tool whose premise is acting as the user), to list only pages the session itself opened plus the one it started on (narrowest, and makes a user-opened tab unreachable even when the task is about it), or to list everything but return only the identifier and let the model spend a switch to see what a tab holds. I lean to listing everything, on the grounds that the tool already drives that browser with the user's real credentials and a URL is less than what a single `read` returns — but it is a genuine widening of what a session can see, and it is your call.

**Should `close_tab` refuse a tab the session did not open?** Same shape as the question above, narrower blast radius, and the failure is worse: closing the user's tab can lose an unsubmitted form with no undo. A rule that a session may only close what it opened is cheap to enforce with the registry this plan already builds.

**Is `navigate` mutating?** I have proposed yes, and it is the one item here with real blast radius: a read-only session would lose browser navigation entirely and be left with whatever page happens to be open, plus `fetch_url` for anything unauthenticated. The argument for is that a name-based classifier cannot distinguish reading a page from firing a URL that is a command. The argument against is that read-only sessions exist for investigation, and investigation on the web means moving between pages. A third option is to classify `navigate` by its argument when the argument is a literal string in the AST, and treat it as mutating only when it is computed — which is more precision than the rest of the classifier has, and would be the only argument-sensitive rule in it.
