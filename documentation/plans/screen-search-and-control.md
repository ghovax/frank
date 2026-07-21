---
created: 2026-07-21T23:41:00Z
updated: 2026-07-21T23:41:00Z
commit: 378873d
---

# Screen Search and Control

This plan reworks how the model perceives and drives a computer or browser, and folds the
project's search tools into one coherent taxonomy. Today `computer` and `browser` are two
tools that expose a live surface as a nested tree the model explores step by step, plus a verb
for every gesture; search is split across three literal tools and has no semantic path. The
directive was to replace the tree-exploration model with **retrieval** (state a goal in plain
words, get the relevant elements back), to let the model **compose** actions instead of calling
one pre-made gesture at a time, and to unify the two surfaces behind one abstraction — with no
backward compatibility and no effort ceiling. The element-encoding decision at the centre of the
retriever was settled empirically, not by taste (see *Decided tradeoffs*).

## Where we are today

**The two automation tools are a tree the model walks by hand.** `computer` (native macOS
accessibility) and `browser` (the user's own Chrome over CDP/Playwright) share the `Surface`
base in `daisy/computer/surface.py` and each expose the same shape: `observe` returns a capped,
nested snapshot; `find` is a *case-insensitive substring* match over name/value/context; `read`
pages text in windows; and then a separate verb for every physical act — `click`, `type`,
`select`, `caret`, `scroll`, `hover`, `choose`, `upload`, `drag`, `press`. Every one of those is
a full model round-trip: read a snapshot, decide, act, re-read the diff. A task that touches
twenty rows is twenty cycles, and the substring `find` cannot bridge "the checkout button" to a
control the page labels "Proceed". The machinery that fights surface churn — settlement polling,
stuck-detection, the actionability-error parser — exists precisely because the model is driving
the rendered surface one brittle step at a time.

**Trusted input is the core strength, and it must survive the rework.** The browser tool's value
is that it acts *as the user*: Playwright's actionability pipeline (visible, enabled, not
occluded) before every click, a real hover-and-wheel scroll routed by the browser's own scroll
chaining, native file-choosers and `<select>` handling, all carrying `isTrusted`. The native
tool has the analogue in AX actions (`AXPress`/`AXConfirm`) and synthesized input. A rework that
dropped to page-level `element.click()` / `value =` would silently regress exactly the hard cases
(overlays, file pickers, native dropdowns, drag, anti-bot) — so whatever replaces the per-gesture
verbs has to keep routing through that trusted layer.

**Search is fragmented and entirely literal.** `web_search` hits the internet; `find_files`
globs names; `search_content` greps file bodies. There is no *semantic* retrieval anywhere —
not over code, and not over the live surface — even though the accessibility tree already hands
us the surface pre-atomised into discrete elements.

**Authenticated API control is latent but unreachable.** The browser tool already has `evaluate`
(run JS in the real, signed-in page) and a `network` reader — but `network` logs only request
*lines* (method, url, status, type), never headers or bodies, so the model can see *that* a page
called `/api/txns` but not what it sent or got back. The one capability that would let the model
pull data straight from a page's own API, riding the real session, is present in pieces and wired
to nothing.

## The design

**One retriever, exposed as two sibling search tools plus a unified screen surface.** The model
states a goal in plain text; a retriever indexes the relevant corpus *at runtime* and returns
ranked hits. The same recipe (model2vec static embeddings + BM25, the [semble](https://github.com/MinishLab/semble)
stack) serves two corpora with two engines chosen for domain fit, and the live surface with a
third:

- **`search_code`** uses semble directly. Its source confirms it is code-specialised —
  tree-sitter *code* chunking, the `potion-code-16M` code model, code-only ranking signals — and
  it indexes straight from disk (`from_path`) with incremental reuse, so the repo needs no
  serialisation and re-indexing an unchanged tree is near-free. This is the tool it was built for.
- **`search_screen`** must *not* use semble: a DOM/AX tree is not code, tree-sitter chunking is
  meaningless on it, and a code model misreads UI labels. It uses the same model2vec + BM25
  recipe with a **general/retrieval** static model (`potion-retrieval-32M`), **one element = one
  document** (the accessibility tree already gives the units, so there is no chunking step), built
  **in memory every turn**. A UI surface is tiny next to a repo, so per-turn indexing is
  sub-millisecond and needs no temp files.
- **`search_web`** is the existing web search, renamed into the taxonomy.

**The element-encoding key is the element's own words, and nothing else.** Each element becomes
one short document: `name`, `description`, `value`, `context`, joined by spaces, empty fields
dropped — the standardised accessibility fields (accname/accdescription on web, `AXTitle`/
`AXDescription`/`AXValue`/grouping on native), never an invented format. `role`, state flags,
`clickable`, and the native handle travel **alongside as structured metadata**, available as
filters but kept out of the embedded text. An element with no accessible name (an icon-only
button) falls back to `role` + `context` and is otherwise reached positionally. `search_screen`
returns the **full, untruncated** text of each hit (it subsumes the old `read`).

**Native handles, obtained natively — no string-built selectors.** A hit carries the platform's
own handle: on web the Playwright aria-ref resolved to a live node (never an assembled
`aria-ref=…` selector string), on native the real `AXUIElement` with its tree path only as a
rebind fallback. Tabs are keyed by their CDP `targetId`, network exchanges by their CDP
`requestId`. The friendly-alias registry that minted `ref1`/`tab1` handles goes away — under
search-then-act the model copies an id straight from a result into the next call, so the raw
native id is what crosses the boundary.

**`control_screen`: one tool, a Python script of bare-name actions, both surfaces.** Acting is a
single tool whose argument is a Python script. Inside it, the primitives are bound as bare names —
`click`, `type`, `press`, `scroll`, `read`, `drag`, `choose`, `upload`, `select`, `caret`, and
web-only `evaluate` and `navigate` — with no `daisy.` prefix. Targets are the native ids a prior
`search_screen` returned. Each primitive routes to the **trusted** layer (Playwright/CDP on web,
AX actions / synthesized input on native), so composability is gained without losing actionability.
Search and act are **two phases**: `search_screen` identifies targets and returns ids; the script
only *acts* on ids it was given. There is deliberately **no `find()` inside the script** — an
element that only appears after an action is reached by another search→act cycle, not by searching
mid-script.

**HAR-style control is just `evaluate`.** Replaying a page's own API from inside the live page —
`evaluate("p => fetch(`/api/txns?page=${p}`).then(r => r.json())", page)` — rides the real
session's cookies, the page's own request-signing JS, and the browser's native fingerprint, which
is why in-page replay works where an out-of-band HTTP client gets a `403`. There is no separate
HAR subsystem: replay is one primitive in the script, and the only JavaScript anywhere is a string
payload handed to the browser (as Playwright's `page.evaluate` already does), not a second runtime
beside Python. To *discover* which endpoint to call, the model searches for it explicitly:
`search_screen` also surfaces the page's captured network exchanges (full request/response), stated
plainly in the tool description and system prompt so the model knows the capability is there —
described, not commanded.

**The script runs in a killable sandbox.** `control_screen` executes the model's Python in a
short-lived **subprocess**; the primitives are thin stubs that RPC to the server, which performs
the real `Surface` action on its serial worker and returns JSON. A timeout kills the child, CPU
and memory are bounded by rlimits, and a crash or runaway loop dies with the child instead of
taking the multi-session server down. The RPC surface is tiny precisely because ids and results
already cross as strings/JSON, not live handles.

**The tool taxonomy.** The surface becomes `search_web` / `search_code` / `search_screen` — three
parallel retrievals over three corpora — plus `control_screen`. `find_files` and `search_content`
are removed; literal name/content search is `bash`'s job (`rg`, `fd`, `find`). `computer` and
`browser` collapse into the search/control pair. Everything else keeps its already-consistent name.

## Decided tradeoffs

**The encoding was chosen by measurement, not intuition.** On a 31-element labelled surface with
20 gold queries, BM25 scored the JSON dump, a formatted template, and the plain words-with-context
join *identically* (top-1 0.85, MRR 0.90) — Okapi's IDF drives the repeated JSON keys, which are
58% of every dumped document, to ≈zero weight, so the lexical "JSON is noise" worry did not hold,
even on queries built to collide with the keys `name`/`value`. But in the mean-pool embedding
space (faithful to model2vec's operation) the same shared boilerplate collapses documents together:
mean inter-document similarity **0.60 for the JSON dump vs 0.03–0.07 for the clean encodings**, an
8–20× loss of separability that degrades the *dense* half we run alongside BM25. The larger effect
was neither — it was **context**: dropping the computed context field (a strict name-only encoding,
or the raw platform snapshot line) cost **~13 points** of top-1, because context is what tells five
identical "Add to Cart" buttons apart. Hence the final key: the words *including* context, JSON and
role kept out. One piece stayed unmeasured — true semantic synonymy of the screen embedder — because
the model host (huggingface.co) is egress-blocked in this environment; it is validated on first run
with model access.

**Two phases, no in-script search — predictability over cleverness.** Allowing `find()` inside the
control script would let a query re-run mid-composition, but it blurs identify-then-act into an
unpredictable interleaving. The cost is that dynamic elements need another search→act cycle; the
gain is that a script is always a straight-line sequence of acts on known ids.

**Trusted input is non-negotiable, so the composition language is Python, not page JS.** Native
apps have no JS runtime, and page JS cannot produce trusted input regardless — so the script runs
host-side in Python with primitives that call the trusted layer, identical across both surfaces.
The one JS touchpoint, `evaluate`, is a data payload to the browser, web-only, and exists for
in-page extraction and replay.

**Subprocess isolation over in-process `exec`.** In-process would be simpler and needs no RPC, but
Daisy is a server hosting live sessions; a control script that can hang or crash the process is
unacceptable, and a subprocess is the only option that makes a runaway loop killable. The marshalling
cost the subprocess usually implies does not apply here because we already decided ids cross as
strings.

**No redaction, by explicit opt-in.** The browser tool is opt-in as a whole; within it the model
sees network headers, tokens, and bodies verbatim. This is a deliberate product decision — the user
authorises the capability — and it deletes an entire fragile redaction layer.

**Behaviour is described, never enforced.** Tool descriptions and the system prompt explain what
each capability *is*, in the present-tense descriptive voice the rest of the harness uses, and let
the model compose freely. No strong strategy ("prefer the API", "always search first") is encoded.

## Testing

The retriever is gated by a labelled query set scored on top-1 / MRR / Recall@3 per encoding, the
harness that produced the encoding decision above; the chosen key must hold its margin over the JSON
and name-only baselines, and re-run once the screen embedder can load to confirm semantic (synonym)
retrieval. Trusted input is verified on the cases page-JS cannot fake — a file-chooser opens, a
native `<select>` changes, a click through a cookie-banner overlay lands, a drag reorders — each
driven by a `control_screen` primitive rather than a synthetic event. The sandbox is verified by a
script with an infinite loop being killed at the timeout, a crashing script leaving the server
healthy, and a normal script's primitive RPCs returning results in order. Cross-surface parity is
one script of the same primitives run against both Chrome and a native app. The HAR path is an
in-page authenticated `fetch` returning JSON from a signed endpoint that an out-of-band client is
refused. The taxonomy is verified by `find_files`/`search_content` being gone, the three `search_*`
tools and `control_screen` present, and `computer`/`browser` retired.
