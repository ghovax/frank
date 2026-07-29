---
created: 2026-07-29T22:30:00Z
updated: 2026-07-29T22:30:00Z
commit: TBD
---

# The Screen Answers Back

A model driving a live surface has to answer three questions, in order: *what can I do here*, *what is there*, and *what happened*. The second of those is the one this project has worked on — retrieval, ranking, what goes in the key, which fields a surface publishes — and it is now good. The other two have never been designed at all. A script is written against a vocabulary that is advertised uniformly and delivered unevenly, it acts into silence, and it learns what it did only by looking again and comparing against a memory it does not have. This plan is about the first and third questions.

## Where we are today

**The tool advertises twenty primitives and one surface implements eight.** A script's namespace is a fixed tuple — `find_one`, `find_many`, `click`, `type`, `press`, `hover`, `scroll`, `choose`, `upload`, `drag`, `select`, `caret`, `read`, `evaluate`, `navigate`, `tabs`, `tab`, `new_tab`, `close_tab`, `frames` — handed out whole regardless of what is answering. The browser implements seventeen of them. A native window implements eight. So a model working in RStudio can write `hover(...)` or `evaluate(...)` or `tabs()`, and learns at runtime, from `The computer surface has no 'hover' action`, that the plan it committed to was never possible.

**One name means two things.** `read()` reads the whole page on the browser and refuses without an element id on a native window. The signature was deliberately unified — `ref` is optional on both, with a comment explaining that a script is written against `read()` rather than against whichever surface happens to be answering — but the behaviour was not. The parameter is optional and omitting it is an error, which is worse than requiring it: the signature promises something the implementation refuses.

**A target is named by a display string.** `control_screen(surface="computer", app="RStudio")` resolves a human-readable application name to a process. With two RStudio instances open this picked the wrong one, and the resulting confusion cost an entire investigation before the cause was found. A display name is not an identity; two windows of one application are indistinguishable through it, and the model has no way to say which it means.

**An action reports what it touched, never what it changed.** `acted_on` carries one record per mutating primitive — the id, name, role and context of the target — which answers *what did I aim at* and not *did anything happen*. So a script that clicks a tab and then cannot find the field inside it has no way to tell whether the click failed, whether the pane is still loading, or whether the field is named something else. In a recorded session against RStudio's Help panel this produced four attempts at a two-step task, and a model that told the user the application's accessibility labels were unstable. They were not: five consecutive reads return an identical element count, identical ids and identical names. The instability was the absence of feedback.

## The shape of the fix

**One vocabulary, over two surfaces, with a target that has an identity, and an answer after every action.** Each of those four is small on its own; together they close the two unanswered questions.

### A target is a window or a tab, and it has a real identifier

**The thing a script acts inside is a window or a tab — never an application.** An application is not a place: it has zero windows or five, and naming one addresses none of them in particular. A window is what has a tree, a title, a focus state and a lifetime, and it is what a person means when they say "that one".

**Neither platform requires us to invent an identifier, because both already mint one.** macOS assigns every window a `kCGWindowNumber` — a system-generated integer, unique per window and stable for its lifetime — which `_displayed_window` already reads today for an unrelated purpose. Chrome assigns every tab a CDP `targetId`, and `web.py` already keeps its own registry keyed by a minted `tab1`/`tab2` on top of it. Using the platform's own identity means two windows of one application are as distinguishable as two windows of different applications, which is exactly the case that broke.

**`target` is required on every call, and there is no frontmost default.** A default that means "whatever the user is looking at" is a race with the user: the screen the model reasoned about is not necessarily the screen it acts on, and the failure is silent and occasional, which is the worst combination. Requiring the target costs one argument and removes a class of bug that cannot be tested for.

**The `surface` argument disappears.** It exists today only to route to `WebSurface` or `NativeSurface`, which is an implementation fact the model should never have been holding. A target id knows what it is; the dispatcher can look it up. This is the concrete form of the rule that the model sees one abstraction: not that the two surfaces behave identically, but that choosing between them is never the model's job.

### The namespace is built from the target

**A primitive that does not exist on a surface is absent from the namespace, not present and failing.** `hover` on a native window becomes `NameError: name 'hover' is not defined` — raised by Python, at the line that used it, before anything else in the script has run — rather than a result payload saying the surface has no such action. The distinction matters because the second reads as a runtime condition the model might work around, and the first reads as what it is: that primitive is not part of this vocabulary.

**The tool description lists the primitives and marks where each one lives.** Documentation and enforcement are not alternatives. The description is what lets a model plan correctly; the absent name is what stops it planning incorrectly for long. Neither is expensive, and having only one of them is how the current situation arose — a description listing everything, with nothing to contradict it until runtime.

**`read` is unified rather than left as two contracts under one name.** `read(target)` returns the whole target's text on both surfaces, and `read(element)` returns that element's. One rule, stated once, true in both places. Today's asymmetry is the clearest instance of the defect this whole section exists to remove.

### Every action answers with what changed

**An action returns a diff, not an acknowledgement.** For each mutating primitive the result carries what actually differs: the acted-on element's subtree, and the cheap global facts — title, focus, selection, and on a browser the url and the network exchanges since the action. These are returned as structured data with the same field names on both surfaces, present when they mean something and absent when they do not, so that learning the shape once is enough.

**The scope is the acted-on subtree plus those globals, not the whole target.** A full re-read after every action is truthful and unaffordable — a browser page is two thousand elements, and a ten-action script would pay for twenty snapshots. The subtree catches the effect of the action itself; the four global facts catch the common cross-screen consequences, which are precisely the ones a subtree diff would miss: a pane switching, a page navigating, focus moving, a selection changing.

**`appeared` is uncapped within that scope.** What became newly present is the single most useful thing the model can learn, because it is what it can act on next, and truncating it means the model cannot tell "there is nothing else" from "there is more I was not shown". Bounding it by the subtree is what makes uncapped affordable.

**Navigation is the exception, and reports a summary instead.** When a `navigate()` or an equivalent pane swap replaces the entire document, the acted-on subtree *is* the new page, and "everything new" is the whole thing. In that case the result reports the new target state — title, url, element count — rather than enumerating a page's worth of elements into the model's context. This is the one place where the general rule would produce something absurd, and it is worth naming rather than discovering.

### Focus is never taken silently

**Acting does not focus the target.** The accessibility API can drive most controls in place, and a background agent that raises windows is one that fights the person using the computer. The default is therefore to act without focusing.

**When an action needs focus, the harness takes it and says so.** Some controls — text entry in particular — will not accept input unfocused, and the failure is silent: `AXPress` on an unfocused control returns success and does nothing. So the fallback is driven by the diff: an input-bearing action that produced no change is retried once with focus, and the result carries `focused_target` naming the window and the reason. Silent focus-stealing is maddening precisely because nothing says it happened; stated in the diff, it is a fact the model can pass on to the user.

**The fallback applies only to input-bearing primitives.** `type`, `press` and `caret` need focus and produce nothing without it. A `click` that changes nothing is usually a click that was already satisfied — a tab that was already selected — and focusing on its behalf would steal the screen to repeat something that had already happened.

**The model can also focus deliberately.** `focus(target)` exists as an ordinary primitive, so a script that knows it is about to do several input-bearing things can take focus once rather than discovering it three times.

### The target list is ambient, and arrives as a diff

**The model always knows what is open, without asking.** The list of targets is carried into context once per turn and refreshed in every `control_screen` result. There is no listing tool and no listing round trip: the cold-start problem that a required `target` would otherwise create — the model cannot call a script to list targets without already having one — simply does not arise.

**It arrives as a diff against the turn's baseline.** A full enumeration on every result repeats twenty unchanged lines to report that one window's title changed. The baseline is established at the start of the turn and each subsequent list reports what was added, removed and changed. This is the same principle as the action diff, applied to the world rather than to one window, and it is why both are worth building at once.

**Only on-screen windows, with system chrome filtered.** A full enumeration of everything running is long and mostly irrelevant — twenty-two windows on a developer's machine, of which most are menu-bar items belonging to Control Center. The filter is the one `_displayed_window` already applies.

**A target that dies mid-script raises, and the error carries the refreshed list.** The model then retargets on its next turn without a separate lookup, because the list it needs is already in the error it just received.

## What this looks like

The task that produced four attempts, written once:

```python
control_screen(target="win-10337", script="""
    click(find_one("Help tab", clickable=True))
    field = find_one("Help search field", clickable=True)
    type(field, "DateTimeClasses")
    press("Enter")
""")
```

```json
{ "ok": true,
  "changed": [
    { "action": "click", "on": {"id": "0.2.1", "name": "Help"},
      "selected": {"from": "Files", "to": "Help"},
      "appeared": [{"id": "0.2.4", "name": "Search help", "clickable": true}] },
    { "action": "type", "on": {"id": "0.2.4"},
      "value": {"from": "", "to": "DateTimeClasses"},
      "focus": {"from": null, "to": "0.2.4"},
      "focused_target": {"id": "win-10337", "reason": "typing requires focus"} },
    { "action": "press", "key": "Enter",
      "title": {"from": "Help", "to": "R: Date-Time Classes"},
      "appeared": [{"id": "0.3.7", "name": "Date-Time Classes", "role": "AXHeading"}] }
  ],
  "targets": { "changed": [{"id": "win-10337", "title": "R: Date-Time Classes"}] } }
```

Every hiccup the model reported is answered by that payload. The pane not being active is `selected`; the field appearing only afterwards is `appeared`; the text landing is `value`; the result page loading is `title`; and the focus it took on the user's behalf is stated rather than silent.

The same shape on a browser, where more facts exist:

```json
{ "action": "click", "on": {"id": "e42", "name": "Find a room"},
  "url": {"from": "https://oakhouse.jp/", "to": "https://oakhouse.jp/eng/house/"},
  "title": {"from": "Oakhouse", "to": "Share houses | Oakhouse"},
  "network": [{"method": "GET", "url": "/api/houses?page=1", "status": 200}],
  "appeared": [{"id": "e88", "name": "Search by Map", "clickable": true}] }
```

And the two failure modes stated plainly:

```json
{ "ok": false,
  "error": "Target win-10337 no longer exists — the window was closed.",
  "targets": { "removed": ["win-10337"],
               "current": [{"id": "win-10412", "app": "RStudio", "title": "analysis — R"}] } }
```

```json
{ "action": "type", "on": {"id": "0.2.4"}, "changed": [],
  "note": "nothing changed and the window is not focused; retried with focus" }
```

## What is deliberately not built

**A `wait_for()` primitive.** The obvious response to "the result was not there yet" is to let the model wait for it, and it is the wrong one: it makes the model guess a duration instead of naming a target, and a guess that is usually right is the hardest kind of bug to find. The diff answers the same question by saying what did change, which is information rather than a delay.

**Symmetry between the surfaces.** A native window has no tabs, no url and no network log. Emulating them would be a lie, and lies of exactly this kind — a name that exists everywhere and means something different in each place — are what this plan is correcting.

**Two tools, one per surface.** `control_browser` and `control_computer` would each list only what it implements, which is tidy, and it would put the model back to choosing between implementations. The target argument already carries that information and does not require the model to think about it.

**A script that can pause and ask the model.** A script suspending mid-run to hand back an intermediate result and resume with an answer would recover from a wrong assumption without losing the rest of the script. It is also directly against an explicit decision in `dispatch.py`, which resolves every human decision in a preflight pass *before* a batch begins, so that nothing prompts mid-execution. More importantly, the action diff removes most of the need: a script stops having to guess what happened between its own steps.

**Instrumented measurement before the build.** Every other conclusion in this project's recent history was settled by measurement, and several confident diagnoses were overturned by it. This design is reasoned instead — from one recorded RStudio session and a reading of the code — which is a sound basis for its shape and a weak one for its quantities. See the caveat below.

## What we know we do not know

**Whether uncapped `appeared` is affordable in practice.** Bounded by a subtree it should be small, and the navigation exception covers the case where it would not be. But "should be small" is an expectation about real applications, not a measurement of them, and a pane that swaps a hundred elements on every click would make the result payload the dominant cost of using the tool.

**Whether the before-snapshot is affordable.** Producing a diff requires reading the subtree before the action as well as after. On a native window that is cheap. On a browser an aria snapshot is not, and a ten-action script pays for it ten times. Restricting the snapshot to the subtree is the mitigation; whether it is sufficient is unknown.

**Whether `changed: []` is a reliable failure signal.** The focus fallback depends on it, and it is a heuristic: an action can legitimately change nothing. Restricting the fallback to input-bearing primitives removes the common false positive — a click on an already-selected tab — but not all of them.

**Whether the per-turn baseline drifts usefully.** If a turn's first `control_screen` call happens minutes after the baseline was taken, the first diff will be large and mostly irrelevant. This is probably self-correcting and is recorded rather than solved.

These four are exactly what a day of instrumented usage would have answered, and they are being taken on judgement instead. That is a deliberate choice about pace, not an oversight, and the first three are the places to look when something in this design turns out to be wrong.
