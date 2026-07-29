---
created: 2026-07-29T10:00:00Z
updated: 2026-07-29T21:00:00Z
commit: TBD
---

# The Input Is the Ceiling

Screen retrieval ranks the elements of a live surface against a plain-language query, and the question that started this work was which words to put in the index. Eighteen encoding strategies later the answer turned out not to be a question about words at all. Every arrangement of the fields we keep lands within a few points of every other arrangement, and the one measurement that separates them cleanly is not an arrangement — it is a field we were throwing away. This plan records what was measured, what was ruled out, what is being changed, and, most importantly, the ceiling that no rearrangement can pass: on a query that describes what a control *does* rather than what it is *called*, nothing we tried exceeds thirty percent, because the page usually does not publish that information at all.

## Where we are today

**The web surface writes `context` into the embedded key, and it is the worst thing in the system.** `web.py`'s `documents()` builds two strings per element: a `shown` text for the model to read and a `key` for the embedding to rank. Both include `context` — the accessible name of the nearest labelling ancestor. `context` exists for a good reason, which is to tell twenty identical "Add to Cart" buttons apart, and in the payload it does exactly that. In the *key* it does the opposite: writing a section's label into every child's key makes all children of a section look alike to a cosine, collapsing the very distinction it was added to draw. Measured against a plain `role + name`, the shipping key is sixteen points worse.

**Link destinations were parsed and discarded.** Playwright's ai-mode aria snapshot emits `- /url: "/wiki/Braille"` as a property line under each link. `_parse_snapshot` matched property lines and `continue`d past them, so the destination never reached a document. A link's URL is the only wording of a target authored independently of the link's visible text, and it is present on eighty-seven percent of elements on a real page.

**The native surface has a different key, and a different answer.** `engine.py` builds its key from name, value and role, with no `context`, so the defect above is web-only. Native rankings also come out differently from web rankings — the surfaces are separate problems and this plan treats them separately.

**Retrieval returns one answer where it usually has the right one just below the top.** On native corpora, recall at rank one was forty-seven percent while recall at rank five was sixty-seven. The answer is more often mis-ranked than missing.

**There is no test suite for any of this.** Every measurement in this document was produced by throwaway scripts in `/tmp` that die with the session that wrote them.

## How this was measured, and what is comparable

Three harnesses were used across five runs, over different corpora and different label-extraction code. **Absolute percentages are not comparable across runs; only comparisons within a run are valid.** This is stated first because the temptation to read a number from one table against a number from another is strong and would be wrong.

**Run 1 — native only.** Eight live macOS applications through `NativeSurface`, roughly 1,160 elements, 270 queries.

**Run 2 — native plus one live web page.** The same eight applications and whichever page the browser had open, 339 queries, with a paired bootstrap.

**Run 3 — eight varied public websites through frank's own parser.** Wikipedia, Hacker News, GitHub, MDN, python.org, BBC News, arXiv and nps.gov, harvested headless so that no live browsing session was touched. 5,119 elements, 797 queries, paired bootstrap over 4,000 resamples. This is the most trustworthy run for the web surface.

**Run 4 — the field inventory.** The same eight sites read through a DOM script rather than the aria snapshot, to enumerate what a page publishes that the parser does not keep. 3,079 elements.

**Run 5 — static against contextual embedding models.** 2,556 elements, 630 queries, four contextual models against the incumbent static one, measuring quality and index latency together.

Every query in Runs 3 through 5 is text a human wrote in the product. Four families were used. **Literal** is the element's own accessible name. **Partial** is a real three-word fragment of a long prose label, standing in for a model that half-remembers what it saw. **Slug** is words from a link's URL, kept only when the URL shares *no word at all* with the visible label, so what remains is genuinely independent wording. **Tooltip** is the `title` attribute, kept only when its words are not a subset of the label — the closest thing to a real semantic benchmark that exists without inventing queries.

## What the measurements found

### Removing `context` from the key is the largest single win

Run 3, fourteen strategies, 797 queries, paired bootstrap against the shipping key.

| Strategy | literal @1 | partial @1 | slug @1 | MRR | vs shipping | 95% interval |
|---|---:|---:|---:|---:|---:|---|
| M `dedup(name + url + role)` | 59% | 53% | 39% | 0.625 | +6.4% | [+4.0%, +8.8%] |
| K `name + role + url` | 60% | 50% | 37% | 0.620 | +5.6% | [+3.3%, +8.0%] |
| L `name + url` | 59% | 51% | 37% | 0.622 | +5.6% | [+3.0%, +8.3%] |
| A `name only` | 67% | 52% | 9% | 0.562 | +2.0% | [+0.0%, +3.9%] |
| H `name×2 + role` | 64% | 53% | 9% | 0.556 | +0.8% | [−0.1%, +1.6%] |
| G `dedup(name + role)` | 62% | 55% | 8% | 0.551 | +0.4% | [−0.4%, +1.1%] |
| I `name + role` | 62% | 54% | 8% | 0.551 | +0.1% | [−0.3%, +0.5%] |
| B `role + name` (shipping, less context) | 62% | 54% | 8% | 0.550 | baseline | — |
| F `a {role} labelled {name}` | 59% | 53% | 6% | 0.532 | −2.3% | [−3.6%, −1.0%] |
| E `name + role + value` | 59% | 51% | 6% | 0.533 | −2.5% | [−3.8%, −1.4%] |
| N everything, deduplicated | 35% | 36% | 35% | 0.490 | −9.8% | [−13.4%, −6.1%] |
| D `name + context` | 42% | 43% | 4% | 0.438 | −12.8% | [−15.8%, −9.8%] |
| C `name + role + context` | 39% | 41% | 3% | 0.419 | −15.3% | [−18.2%, −12.5%] |
| **J the shipping key** | **39%** | **39%** | **3%** | **0.418** | **−16.1%** | **[−19.1%, −13.2%]** |

Every strategy containing `context` occupies the bottom of the table. The three at the top are the three containing the URL. Nothing else separates from the baseline.

### The field inventory: what a page publishes and what we keep

Run 4, 3,079 elements across the same eight sites. "Novel" counts elements where the field contributes at least one word not already in the visible text.

| Field | Present | Novel | Kept today |
|---|---:|---:|---|
| `landmark` | 91% | 91% | no |
| `href` | 87% | 87% | only after this plan |
| `heading` | 84% | 83% | approximated by `context` |
| `class` | 42% | 42% | no |
| `idAttr` | 28% | 28% | no |
| `title` | 27% | 20% | no |
| `dataAttrs` | 14% | 14% | no |
| `ariaLabel` | 9% | 9% | yes, as `name` |
| `colHeader` | 8% | 8% | no |
| `labelledby` | 5% | 0% | folded into `name` |
| `inputType` | 3% | 3% | no |
| `alt`, `inputName` | 2% | 2% | no |
| `placeholder`, `describedby` | <1% | <1% | no |

Coverage turned out to be a poor predictor of value. `landmark` is present on ninety-one percent of elements and is worth nothing, because every element inside a navigation shares it and a field that does not vary cannot discriminate.

### What each discarded field buys when added to the key

| Strategy | literal | partial | slug | tooltip | MRR |
|---|---:|---:|---:|---:|---:|
| label only | 76% | 73% | 11% | 14% | 0.588 |
| label + role | 73% | 72% | 12% | 19% | 0.580 |
| M `label + url + role` | 69% | 70% | 37% | 23% | 0.646 |
| M + `heading` | 62% | 65% | 35% | 30% | 0.621 |
| M + `landmark` | 63% | 73% | 38% | 22% | 0.637 |
| M + `alt` + `colHeader` | 68% | 69% | 36% | 24% | 0.638 |
| M + `id` + `class` + `data-*` | 58% | 75% | 32% | 24% | 0.611 |
| M + `title` | 68% | 66% | 36% | *53%* | *0.676* |

The `title` row is marked because its tooltip score is **circular by construction** — the query *is* the title — and must never be quoted as a generalisation result. It is reported only because the gap between it and everything else measures something real: the distance between what a page says and what a rearrangement of the page can recover.

### Contextual embeddings are slower and no better

Run 5, on the same benchmark, measuring index time for the whole corpus on CPU alongside quality.

| Model | Parameters | literal | partial | slug | tooltip | MRR | Index |
|---|---:|---:|---:|---:|---:|---:|---:|
| **M2V_multilingual_output** (static) | — | 75% | 75% | 40% | 19% | **0.683** | **0.05s** |
| bge-small-en-v1.5 | 33M | 72% | 60% | 42% | 28% | 0.655 | 2.52s |
| multilingual-e5-small | 118M | 75% | 66% | 36% | 24% | 0.653 | 3.07s |
| all-MiniLM-L6-v2 | 22M | 75% | 59% | 39% | 26% | 0.646 | 1.36s |
| paraphrase-multilingual-MiniLM-L12 | 118M | 68% | 52% | 32% | 22% | 0.580 | 2.89s |

The trade is close to symmetric: a contextual model buys roughly nine points of semantic retrieval and sells roughly nine points of fragment matching, for twenty-seven to sixty times the index cost. The reason is that most real queries here are short label fragments where token overlap *is* the correct signal, and a static embedding is in effect a very good bag of words. Contextual understanding pays only on the tooltip family, which is the smallest one. The latency finding is beyond doubt; the quality ordering was not bootstrapped and should be treated as suggestive.

### The ceiling

Across every non-circular strategy in Run 4, description-style queries land between fourteen and thirty percent. Indexing a real human-written description reaches fifty-three. That twenty-three point gap is the information the page never wrote down, and no combination of fields we already have can recover it, because seventy-three percent of elements carry no description of their purpose at all. A page states that a button is called "Submit"; nothing in the DOM states that it files your tax return. **This is the ceiling, and recognising it is the main result of this work.** Every remaining encoding decision moves single digits underneath it.

## What is being changed

**The web key becomes `dedup(name + url + role + title)`.** This is strategy M, the best of the fourteen and one of only three separably better than the shipping key. It costs seven points of literal accuracy against a bare name and buys twenty-six points of slug accuracy, which is judged the better trade because a query naming a destination is a query a bare label can never serve.

**`context` leaves the key and stays in the payload.** The model continues to read it; the cosine stops being poisoned by it. This is the sixteen-point item and it costs nothing.

**Link destinations are parsed.** `_parse_snapshot` now attaches a `/url:` property line to the element it is indented under, guarded by a depth comparison against the most recently kept element so that a property line cannot attach to an unrelated ancestor.

**`title` is captured through a second DOM pass.** This is the one change whose benefit could not be measured non-circularly, and it is being made on judgement rather than on evidence. It is also more work than it first appeared: Playwright's aria snapshot *drops* the `title` attribute whenever an element also has visible text, which is precisely the twenty percent of cases where `title` carries new words, so capturing it requires reading the DOM separately and joining the result back onto aria-refs.

**Retrieval already returns more than the top one, so nothing changed.** `find_many` defaults to a limit of eight and `find_one` selects score-competitively from `scored[:5]`. This was written down as a decision before the code was read, and reading it showed the work was already done — recorded here because a plan that quietly drops an item looks the same as a plan that forgot one.

**`value` leaves the key.** It was in the first implementation of `web_element_text` despite an earlier sweep having already measured it as harmful, and the harness caught it on its first full run: removing it is worth +1.4%, 95% interval [+1.2%, +1.7%]. The lesson is not about `value` — it is that the harness earned its cost before it was even committed.

**The native key becomes the name alone.** This reverses the decision recorded above it, and the reversal is worth reading as a warning rather than as a correction. Native was left alone on the grounds that its candidates were within noise — a judgement resting on two runs whose queries had been built by joining an element's name to its role, which is the construction that flatters any key containing a role. Once ten live applications were recorded and queried only with what those applications actually wrote, the picture was not close: ranking on the name alone beats name-with-role-and-value by 2.5%, 95% interval [+0.8%, +4.2%], separable. `role` and `value` were weight in the embedding without being what a query asks by. Both still reach the model in the payload, and `find_one` filters on `role` exactly rather than approximately.

**`role` leaves the browser key too**, at +0.8% [+0.3%, +1.3%]. The caveat stands and is deliberately not resolved: no query family ever names a kind of control, which is the one thing a role is for, so the measurement was taken on queries that could never have rewarded it. What the logged queries say about how often a real query names a control kind is the evidence that should reopen this.

**`find` queries are logged so that the real query mix can be learned.** The relative weight of the four query families is currently my invention, and it is the assumption on which the choice between M and a bare name rests. Until real queries are recorded, that choice is provisional.

### The native surface, measured at last

Ten live applications recorded through `NativeSurface` — Finder, Photos, Terminal, System Settings, Code, RStudio, Claude, Skim, Reminders, Anki — for 1,197 elements and 518 queries. Only the literal and partial families apply, since a native window publishes neither a link destination nor a tooltip.

| Composition | literal | partial | recall@5 | MRR | vs the key that shipped |
|---|---:|---:|---:|---:|---|
| `name` | 87% | 79% | 96% | 0.904 | **+2.5% [+0.8%, +4.2%] separable** |
| `name + role` | 86% | 79% | 96% | 0.899 | +1.4% indistinguishable |
| `name + role + context` | 86% | 79% | 96% | 0.899 | +1.4% indistinguishable |
| `name + role + value` | 85% | 78% | 95% | 0.890 | +0.2% indistinguishable |
| the key that shipped | 85% | 77% | 95% | 0.889 | — |
| `name + value` | 85% | 65% | 95% | 0.877 | −1.9% indistinguishable |

Native retrieval is a far easier problem than web retrieval — 87% against 52% on exact labels — because a native window holds a hundred elements where a page holds two thousand, and few of them share a name.

## What the implementation changed about the measurements

**Query families are no longer sampled.** The first cut of the harness took a fixed number of queries per family per site, which silently *imposed* equal weighting across families — the one assumption in this investigation with no evidence behind it, hard-coded into the instrument meant to test it. Every family now yields every query its pages support, so the mix is a property of the corpus rather than a knob, and the sampling seed is gone. The query count rose from roughly 800 to 6,229, and the context finding grew with it, from −16.1% to between −14.3% and −19.4% depending on the base it is measured against.

**Recall depth and bootstrap resamples became parameters.** Both were constants, which made the harness quietly about one choice of each. Recall is now reported as a curve across depths 1, 3, 5 and 10, which answers a question a single depth cannot: whether a strategy misses elements or merely mis-orders them. Across every composition the curve rises steeply from rank 1 to rank 5 and then flattens, so the misses are orderings, not absences.

**Strategies are compositions of fields, named after their fields.** There is no "current" or "previous" strategy in the harness vocabulary. Tests assert that *adding the section label costs accuracy* rather than that one arrangement beats another, so they stay meaningful when the arrangement changes. The live key is measured alongside them by calling the product's own function, so it cannot drift into a stale copy.

## What is deliberately not built

**Contextual embedding models.** Measured, slower by a factor of twenty-seven at best, and not better. The requirement that a replacement be *as fast* as the incumbent eliminated the entire category before any quality argument was needed.

**Generated descriptions.** The only lever with real headroom, and the only one that adds information rather than permuting it, since it could reach the twenty-three point gap that the field inventory cannot. Ruled out on cost: it means a model call per element per page.

**Query rewriting.** Rewriting the agent's query into label-like form before searching would meet the index where it is, and was never measured. Ruled out by decision.

**`landmark`, `id`, `class`, and `data-*` in the key.** Machine tokens cost eleven points of literal accuracy and buy nothing; `landmark` is present nearly everywhere and discriminates nothing.

**`heading` in the key.** It trades seven points of literal for seven points of tooltip, one for one, and literal queries are judged the more common case. It remains available in the payload.

**`value`, `alt` and `colHeader` in the key.** Separably worse, at −2.5%.

**Multi-vector indexes.** Embedding three or four phrasings per element and taking the best match was measured on native corpora and produced no gain over a single phrasing.

**Role synonym expansion.** Mapping "button" to "button press click" cost five points of literal accuracy.

**Reciprocal rank fusion.** Previously removed; dense ranking with BM25 as a fallback is what remains, and nothing measured here argues for bringing fusion back.

## The measurement harness

Everything above was produced by scripts that no longer exist, which is not acceptable for numbers this load-bearing. The harness moves to `tests/retrieval/` and is written to be maintained rather than thrown away: full descriptive names throughout with no single-letter variables anywhere, module and function docstrings explaining what is measured and why, ordinary prose comments, and a structure that admits a new strategy or a new query family without rewriting the evaluator.

**Corpora are cached fixtures, not live harvests.** A test that reaches eight websites is flaky, slow, and produces different numbers every week as those sites change. The harvester is kept as a separate, explicitly-invoked tool that refreshes the fixtures; the test suite reads what it committed.

**The evaluator is separate from the strategies and from the query builders.** A strategy is a named function from an element to its indexed text. A query family is a named function from a corpus to a list of query-and-target pairs. The evaluator knows about neither and reports accuracy at rank one, recall at rank five, and mean reciprocal rank per family.

**Differences are reported with a paired bootstrap.** Most of the encoding differences in this investigation are noise, and a harness that prints a ranked table without intervals invites exactly the mistake made repeatedly here — reading a two-point gap on a few hundred queries as a result.

## Errors made during this investigation

Recorded because they were expensive, and because two of them were the same mistake twice.

**Queries were built as `"the {name} {role}"`, which baked the role into the query.** Any encoding containing role text then scored well by construction. This rigged Runs 1 and 2 and produced a recommendation — `name + role`, hotkey stripped — that was reported confidently and is void.

**The accessibility path was tested against Chrome and the result reported as "we cannot see inside web pages at all."** Web content has never come through the accessibility tree; it comes through the CDP browser surface, which returns over a thousand elements from a real page and always did. `documentation/tools.md` states this plainly. The false finding was then ranked above every real one.

**A sweep silently skipped the web corpus when the page was mid-navigation and reported a native-only result as if complete.** This happened roughly an hour after the pattern it exemplifies — failures that destroy their own evidence — had been described in this same investigation. The harness now aborts rather than degrading.

**Fifteen semantic queries were invented and their results quoted.** A hand-written query set measures the imagination of whoever wrote it. Every semantic number in this document now comes from `title` attributes, which developers wrote.

## Open questions

**The real query mix is unknown, and it decides the web key.** Strategy M beats a bare name on a family weighting that was chosen, not measured. Query logging is what settles it, and until then M should be understood as provisional rather than final.

**Whether capturing `title` actually helps cannot be measured with the data available.** Its benefit is circular against the only benchmark that exists for it. The change is being made because a human-written description of a control is obviously useful, not because a number says so.

**Whether `role` belongs in the key is now genuinely open, and the harness cannot settle it.** Dropping it measures as *better* by 0.8% (95% interval [+0.3%, +1.3%]), which is separable. But every query family is built from an element's own words — a name, a fragment of one, a URL, a tooltip — and not one of them ever names a kind of control, which is the single thing role is for. Removing it on that evidence would be letting the benchmark's blind spot make the decision, which is the mistake this investigation has already made twice. It stays until the logged queries say otherwise.

**The semantic ceiling stands unaddressed.** Both levers that could raise it — generated descriptions and contextual models — are ruled out, one on cost and one on measurement. If description-style queries turn out to be common in the logs, that decision deserves revisiting with real numbers behind it.

**The native surface has never been measured with a clean query set.** Its two runs both used the rigged query construction. Its key is being left alone on the grounds that its candidates are within noise, but that judgement rests on compromised data.
