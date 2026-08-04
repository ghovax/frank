Read and drive the live screen by composing a short Python script — one program that both finds elements and acts on them.

The script runs against one **target**: a window or a browser tab that you name. Everything goes through `screen`, an object already bound to that place. Your context lists the targets. You never say which kind of thing a target is, because the id already says it.

Only the primitives that target supports exist in the script. To reach for one it does not have raises a `NameError` on that line, before anything else runs.

Reading happens in the script. `find_many` and `find_one` rank the surface by meaning and return elements. Each element is a dict with a stable `id`, its `role`, its text, and its `context`. Acting is trusted input: a click is a real click, the harness checks that the element can be acted on, it works through an overlay, and it opens a file picker or a native dropdown. Typing fires the events that pages listen for.

Because the script is ordinary Python, a whole task fits in one call. Loop over rows, branch on what you find, and call the page's own API in one line, instead of one round trip for each action.

## Finding elements

`find_many` returns the ranked matches, to read or to harvest a whole set. `find_one` returns the single best match, and raises where its top matches cannot be told apart. That is what makes `find_one` the one to use before you act.

**`clickable` is optional. Leave it out unless you are sure.** Pass `True` where you want something you can act on and text would get in the way, as with "the Save button". Pass `False` where you want the words on screen and controls would get in the way, as with "the filename shown in the row". Omit it — the default — wherever you are unsure, or you want both. It asks about your own intent, not about how the platform spells its widget names: a tab, a link and a checkbox are all `clickable=True`.

Ranking happens *inside* whatever you narrow to, so to narrow beats to lengthen the query. Similarity has no notion of what a thing *is*, and "the search button" competes against every element that merely mentions search.

`name` matches exactly. `context` matches by containment, and it exists only on a page — a window carries no context, so that facet admits nothing there and the search quietly widens to everything.

`near=` says *where* an element is, which a query alone never does. It takes a second plain-language query, finds that element, and prefers the candidates beside it. The control goes in the query, and a unique neighbour goes in `near=`, as in `find_one("the toggle", near="the label shown in that row")`. Reach for it wherever an interface repeats a control: the rows of a list, the tabs of a bar, the cells of a table. It is how you name one of several, not a fallback for a query that failed, and it does no harm to a query that would have succeeded alone. Anchor on something the surface says exactly once. Where the anchor is itself ambiguous, the find refuses instead of guessing.

## How to word a query

A find ranks two ways at once: by what the words *mean*, and by how they are *spelled*. It leans on spelling for a short query and on meaning for a long one. That is one rule with four consequences, and they are worth holding in mind, because the difference between the best and the worst way of asking for the same element is larger than the difference between any two elements.

- **Quote the label where you can see it, and quote it exactly as short as it appears.** A quoted label is the strongest query there is. You do not have to clean it up first. Case, punctuation, spacing, singular against plural, an ampersand written as "and", a number retyped without its separators, a trailing ellipsis or a keyboard shortcut left on or taken off — the ranker absorbs all of these, and none of them is worth a second attempt. Pass the label through as you read it.
- **A label the interface cut off is still worth quoting, as far as it goes.** A sidebar, a tab and a narrow column truncate mid-word. Send the fragment you can see, instead of guessing the rest. A wrong guess is worse than a short quotation, because the ranker matches on the ending you invented.
- **Where you cannot see a label, describe the purpose in a full sentence, not in two words.** A short invented phrase is the weakest query of all: it is too short to describe anything, and too made-up to match any spelling. Either quote what is on screen, or say what the thing is *for*, in the words you would use to explain it to somebody. Do not abbreviate, and do not send initials. An acronym you constructed matches almost nothing.
- **Name the kind of control. It helps, and it costs nothing.** Write "the Commit *button*", "the search *field*", "the Plots *tab*". Applications publish what kind of thing each element is, and to say it narrows the field for free. Say it whenever you know it.

Expect a description to be weaker than a quotation, and plan around it. Where you describe rather than quote, reach for `find_many` and choose from what comes back, instead of `find_one` and hope.

**Where two elements are written identically, no wording reaches one of them.** This is the commonest reason a find is wrong, and it is not a ranking problem. An application publishes ten rows whose entire text is "text", or four buttons that all read "Close", and every one of them is an equally correct answer to any query you can compose. To reword cannot help, and neither can trying harder.

What separates them is where they sit. So use `near=` to name a unique neighbour, or take `find_many` and pick by the `parent` and `bounds` each hit carries. `find_one` refuses instead of guessing when its top candidates cannot be told apart, and it hands you exactly those fields to choose by. That refusal is the tool working. The answer to it is a discriminator, not another phrasing.

## Acting on what you found

**Act on the element a find returned, not on its id string.** Every acting primitive takes the result object itself: `result = screen.find_one("the Save button"); screen.click(result)`. Reach for that form. The id string works too, but it names the surface *as it was when that find ran*. A page that navigates or re-renders has new ids, and a stale id fails on the line that uses it instead of quietly finding something else. To pass the object keeps the find and the action in step. Where a find returns several and you pick one in Python, pass the dict you picked, not `picked["id"]`.

**A hit carries where it goes, not only what it says.** Beyond `id`, `role`, `name`, `text` and `context`, a link or a result on a page carries **`url`**: the destination the page published for it. That is the strongest identity a result has, because the site writes it rather than rendering it for a reader. Two entries that read "OpenAI" are told apart by one of them going to `/OpenAI`. Prefer `url` to a comparison of display text whenever you choose between similar-looking results. An element also carries the states the platform reports — `checked`, `disabled`, `expanded`, `selected`, `pressed`, `active` — and `bounds` for where it sits.

**Typing tells you what landed.** `type` returns `value`: the text read back out of the field after the keystrokes, not what you asked it to type. That is the confirmation to check. Where `value` holds your text, the text is in the field. Do not read `changed: []` as a failure. `changed` reports what else on the surface moved, and a field that accepts text without the page reacting yet is an ordinary success. Where `value` differs from what you sent, the result says so with a note, as with a field that clamps or reformats its input.

**`limit` is how many you want, and small is right.** It defaults to 8, and it is capped. A find is a ranked search, so to ask for fifty gets you the eight that matched and forty-two that did not, and every one of them is spent context. Raise it only where you genuinely harvest a set you will filter yourself — the rows of a table, the items of a list — and leave it alone otherwise.

**`find_many` returns `[]` where nothing rises above the noise.** That is an answer, not a failure. The view may still be building, you may be on the wrong target, or the thing may not be there. It is not evidence of which. Measured against queries whose element really had been taken off the screen, the ranker is close to a coin toss at telling "absent" from "present but worded unlike your query". So wait for a view that loads, check the target, or quote a visible label in place of a description. Do not run the same query again unchanged, and do not report the thing as missing on an empty result alone.

The converse does not hold either. A *non-empty* answer does not prove that what you asked for exists, because a confident wrong match scores like a right one. Where it matters, check what came back — its `role`, its text, where it sits — instead of trusting that a match was returned at all.

## The page's own traffic

On a page, a find also searches the page's network requests and WebSocket frames, so you can find the endpoint behind a rendered view instead of walking the DOM.

**A captured exchange carries its shape, never its data.** It gives `method`, `url`, `status`, the header *names*, and each body described field by field with every value replaced by its type, as in `{"data": {"items": [{"id": "int", "text": "str"}]}}`. That tells you what an endpoint returns and what to send it. Where you want the values themselves, replay the request in the page with `evaluate`. That runs with the page's own session, and it gives you real data in the script instead of a copy of it in your context.

## The primitives, and the script

The primitives and their exact signatures arrive in your context each turn, under `primitives`, keyed by what each place `can` do. Read them from there and not from memory. The harness generates them from the code, so they cannot be out of date, and a place is only ever offered what it may actually run — under a read-only policy the acting primitives are simply absent. To reach for something a place does not have fails **on the line that uses it**, and the surface names what the place does have, so everything above that line already happened. Check the vocabulary before you commit to a plan, not after.

**The script starts with an import.** Its first line is

    from frank.screen import screen

and every screen call goes through that object: `screen.click(...)`, `screen.find_one(...)`, `screen.type(...)`. Nothing is put into scope for you, so a script that skips the import fails on its first use of `screen`. That is deliberate: a program which declares what it depends on is one you can save to a file, import later, and hand to somebody who never heard of this harness. There are no bare-named primitives, and `click(...)` alone is an undefined name. `screen` is already bound to the target you named, so there is nothing to open and nothing to pass. The signatures in your context are written the same way.

**You write Python, and it is a real program.** It is not a macro and not a step list. It is a module body, and you may `import` whatever the task needs: the standard library, a workflow somebody saved, or the script package a skill ships.

Imports are not restricted. What the script can *reach* is settled by the operating system before it starts: the process it runs in has no network, and it can write nowhere that outlives it. What it may *do to the screen* is settled by the surface, which refuses a primitive this session may not use however the call was spelled.

So everything Python gives you is on the table: loops, conditionals, comprehensions, `try`/`except`, functions, data structures, and computation over what a find returned. `screen.wait_for(query, seconds=...)` blocks until something matches, which is how you say "once the pane has loaded" instead of hoping. It succeeds the moment the thing appears, and it tells you plainly when the thing never does. Reach for it instead of a pause for a guessed number of seconds. For an interval that answers to nothing on screen, `time.sleep` is an ordinary import like any other.

The failure to avoid is three timid lines and another round trip to learn what the fourth should be. Nothing carries between calls except element ids, so each new call starts blind. Write the program the task actually needs.

## Saving work

**A workflow can be a file.** `screen` is an instance of the importable `frank.screen.Screen`, so the same calls work in a saved module as they do inline. The shape is ordinary Python, and nothing about it is special to any one task:

    # .agents/workflows/<name>.py
    from frank.screen import Screen

    def <what_it_does>(screen: Screen, <what_varies>: str) -> <what_it_gives_back>:
        # One sentence saying what this does — a real docstring here, in the file itself.
        ...
        return ...

That generalises to anything. `screen` comes first, whatever changes between runs becomes a parameter, and the function *returns* what the caller needs instead of printing it.

Two directories hold workflows, and both import as `workflows`. `.agents/workflows/` sits in the project, versioned with it, for work about *this* codebase's application. `~/.agents/workflows/` holds the person's own tools; it is available everywhere and committed nowhere. That distinction matters: a workflow that drives somebody's mail carries their account names and their habits, and it does not belong in a shared repository. Ask which one they want where it is genuinely ambiguous, and say which you chose where it is not.

A **skill** can carry screen work the same way, and for anything larger than a single function that is the better home. A skill's `scripts/` directory is a real Python package with its own `pyproject.toml`, and it sits on your import path, so `from <package> import <function>` reaches it from a script. Read the skill's own `SKILL.md` for what it offers, before you write what it already does.

Whatever exists arrives in your context under `workflows`, with the import line, the call, and what each one does. When you work something out that is worth having again, save it with `write_file`. The `ran` trace is exactly what happened, so you record rather than reconstruct.

The harness reads what you import along with your script, when it decides whether to ask the user. A workflow or skill package that only reads keeps your script read-only. A module that cannot be read from here, such as a third-party library, means the user is asked once.

## Two places to compose

**There are two places to compose, and they are peers.** In the script, Python composes the primitives: loop over what a find returned, branch on it, wait for what an action reveals, compute the answer, report once. On a page, `evaluate` composes inside the document: one expression can filter a table to the rows that matter, aggregate a list into a number, read the page's own state, or call its signed-in API with `fetch` through the user's real session.

Neither is the fallback for the other, and the strongest scripts use both. Use `evaluate` to work out *what* to act on, and the element primitives to act on it. The harness classifies `evaluate` as state-changing, because nothing reading a script can tell a query from a mutation, so a read-only policy does not offer it. There, `find` and `read` are the way.

## Targets and elements

`element` — the first argument of an acting primitive — is an id a find returned, the find result itself, or a plain-language query. It is never the `target`, which names the *place* the script runs in and is fixed for the whole script.

How a query resolves depends on the verb. `click`, `type`, `choose`, `upload` and `drag` resolve it the way `find_one` does, so an unclear query is caught instead of guessed. `read`, `hover`, `scroll`, `caret`, `select` and `focus` take the top match without that check. For anything that changes state, prefer an id you already hold. Both `screen.press("Enter")` and `submit=True` post a form, so be deliberate.

**Targets are the places a script runs.** Every window and tab has an id that the platform mints, and the current list arrives in your context each turn. Pass the one you mean as `target`. There is no default, because "whatever is in front" is a race with the person using the computer. An application is not a target: two Finder windows are two places, and to name the application cannot say which. Where you name a target that is not on the list, the current list comes back, so you can pick another without a lookup.

A tab appears in that list only once a browser session exists. Until then a browser contributes its *windows*, which are addressable exactly as any other place, and to list them never opens a connection or puts a consent prompt in front of the user.

Each entry carries what it takes to tell one place from another, so read it before you choose:

  - `app` and `title` — who owns it, and what it calls itself. Neither is unique: two Finder windows are both "Applications".
  - `can` — the vocabulary this place answers to, which keys into the `primitives` map beside the list.
  - `document` — the file or page it holds, as a plain path or URL. This is the strongest discriminator there is, and usually the honest way to name a window to the user, as in "the window showing report.pdf".
  - `main` — the application's main window, where the application says which one that is.
  - `bounds` — where it is and how big, in screen points. This separates two windows that agree on everything else.
  - `visible: false` — behind others, minimized, or on another desktop. You act in it exactly as in any other place. Say so to the user when you do, because they cannot see it happen.
  - `addressable: false` — an application that publishes no windows to accessibility. It names the application, not a place, and you cannot act in it.
  - `focused` — where the user's keyboard is right now. Somebody works there.

## What changed

The primitives that alter something — `click`, `type`, `choose`, `upload`, `drag`, `press`, `navigate`, `caret`, `select` — each answer with what they *changed*, and not merely with what they touched. They report the globals that moved: `title` and `focus` everywhere, `selection` on a window, `url` on a page. They report what became newly present as `appeared`, with `appeared_total` where there was more than a sample's worth.

A primitive that replaced the whole document reports `navigated` — the new title, URL and element count — instead of listing a page's worth of elements at you. One that changed nothing says so with `changed: []`, which is your signal that the click missed or the pane had not loaded, rather than that the element was named differently. The reading primitives report no such record, so their silence means nothing either way.

## Tabs and frames

The browser has more than one page, and you choose which one you are on. `tabs`, `tab`, `new_tab` and `close_tab` are in the page vocabulary, with their signatures.

These are the user's own tabs, open because they work in them. Read and switch freely, but leave a tab you did not open, unless the task is about it. To close one can throw away a half-filled form, and there is no undo.

An iframe is its own document, with its own origin and its own session. `frames` lists them. Element ids are already frame-scoped, so `f1e3` is the third element of frame `f1`, and to act on it needs no extra step. But to read or script a frame does need the frame named, and that is the only way to reach an embedded checkout, consent screen or viewer through the credentials it holds.

## Reading, and the result

**`read` gives you words. `find_many` gives you elements.** Reach for `read` where you want what a place *says*. It answers with the text, one entry for each label, and nothing else: no ids, no roles, no positions. Reach for `find_many` for anything you compute over, because its entries are dicts that carry `id`, `role`, `text`, `context` and `bounds`. Those fields are what let a script filter to one pane, sort by position, count what matched, or act on what it found. A script that reads everything first, and then has to ask again because the text it holds cannot be told apart, has spent a round trip on what a single `find_many` would have handed it in a usable shape.

The script runs like a notebook cell. The value of a trailing bare expression is reported as the result, and whatever you `print` comes back too. The result lists what each action *changed*, under `changed`, so you can confirm that the click landed rather than only that you aimed it.

Where the surface cannot be read — Accessibility not granted, or the browser not connected — that comes back as an error to raise with the user. It is not something to route around.

Arguments:
  - script: The Python to run.
  - target: The window or tab to run it in, by the id from the target list. Required.
  - explanation: Why the task needs this.
  - risk: How much damage this could do. Higher for an action that changes state.
