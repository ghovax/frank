# A Typed Turn Core

This is a maintainability plan, not a feature. The A2A-compliance work landed a capable harness, but it grew its turn engine as two enormous methods over untyped dictionaries, and each new capability was threaded through them by hand. The result is high-entropy: `AgentRuntime.stream` and `HarnessAgentExecutor.execute` are hundreds of lines of tangled control flow; a turn's durable state is scattered across stringly-typed `task.metadata` keys; the runtime↔executor contract is an enum tag plus an untyped `**data` bag; "single source of truth" is aspirational rather than real; and the frontend reads the same events as `Record<string, unknown>` and hardcodes user-facing strings in hand-written dictionaries. This plan proposes a *small* set of typed primitives, makes them the load-bearing core, and adapts everything else to fit them — so the next change is a small edit to a typed structure, not surgery on a method too big to test. It builds on [`unified-durable-turn.md`](./unified-durable-turn.md) (which consolidated the persistence but left the god-methods intact) and the findings in [`audit.md`](../audit.md).

## Where we are today

The entropy is concrete and nameable.

**Two god-methods.** `stream()` and `execute()` each interleave a model call, a preflight pass, tool execution, suspension, compaction, steering, and error handling, with `return`/`yield`/`continue` scattered through a fifteen-branch event dispatch. They are too large to restructure safely: unifying the delegated pause in [`unified-durable-turn.md`](./unified-durable-turn.md) had to stop short of the clean version precisely because the executor loop could not be wrapped without re-indenting a method with returns woven through it. A method that is too dangerous to refactor is already too big.

**Blob-soup state.** A turn's durable control-state lives in `task.metadata` under bare string keys — `PENDING_INTERACTION_KEY`, `TURN_KIND_KEY`, `referenceTaskIds`, `agentLane` — read with `metadata.get(...)` and written by dict-spread. There is no schema; a typo is a silent bug; the shape is discoverable only by grepping.

**Untyped events.** The runtime yields `StreamEvent(Type, **data)` — an enum tag plus an open bag — and the executor reads it with `data.get("command", "")`, `data.get("id", "")`. The contract between the two files lives only in their shared conventions; adding an event kind that a consumer forgets to handle fails silently.

**"Single source of truth" that isn't.** The durable-turn work moved the conversation into the task store and *said* the task store is the one surface, but a turn's state is really in four places: the `turn_checkpoint` table, `task.metadata` blobs, the `task_history` wire view, and a wholly separate `background.db`.

**The frontend mirrors the mess.** `reduceDataPart` switches on `String(data.kind ?? "")` and coerces every field with `String(data.x ?? "")`, even though a generated `events.ts` union already exists and goes unused at the read sites. `message.meta` is a `Record<string, unknown>`; `ToolCall` declares `permission`/`question`/`onPermission` props it ignores; and user-facing strings live in hand-written dictionaries (`AGENT_STATE_LABEL_KEY`, tool-status labels, question-option shapes) — hardcoded English, outside i18n.

The unifying disease: **untyped dictionaries used as contracts, and control flow too large to see.**

## The core idea

Introduce a handful of typed primitives, make them the only way turn state is represented and moved, and reduce the god-methods to thin drivers over them. Four on the backend, one on the frontend, and one rule that keeps them honest.

- **`TurnRecord`** — one typed, serializable value that *is* a turn's durable control-state.
- **`TurnEvent`** — a closed, typed union that *is* the runtime↔executor contract.
- **`TurnMachine`** — an explicit state machine that replaces the tangled control flow; suspension and resume are states, not scattered `return`s.
- **`TurnStore`** — one facade that owns turn persistence, with an honestly-named `JobStore` beside it rather than a hidden fourth store.
- **The frontend event/label model** — the generated union as the *only* reader of parts, and i18n for every user-facing string, killing the ad-hoc dicts.

And the invariant that prevents regression: **no untyped dictionary crosses a module boundary, is persisted, is put on the wire, or is rendered to a user.** A dict is fine as a local, transient computation; it is never a contract or a label.

## `TurnRecord`: the one typed control-state

Replace the `task.metadata` string-key soup with a single pydantic `TurnRecord`, serialized under one key and read/written only through typed accessors: the turn `kind` (an enum, from which the restart policy is derived — not a second field), the `pending` interactions as a typed `list[PendingInteraction]`, the resolved `decisions`, the `lane` path, and the `parents`. The `PendingInteraction`, `ToolGate`, and `ToolPlan` values already exist as dataclasses; `TurnRecord` is where they finally live together instead of being flattened into `metadata["pendingInteraction"]["gates"][0]["request_id"]`. Every site that poked `task.metadata` — the suspend handler, the resolver, the reconciliation, the relay — goes through `TurnRecord`, so the reconciliation reads `record.kind`, not `str(metadata.get("daisyTurnKind", ""))`, and a missing field is a validation error at the boundary rather than a `KeyError` three calls deep.

The large, write-cadence-sensitive part — the conversation checkpoint — stays out of the record (in its own table, referenced by task id), exactly as the durable-turn work already separated it; `TurnRecord` carries the reference, not the payload, so it stays small and cheap to rewrite.

## `TurnEvent`: the typed runtime↔executor contract

Replace `StreamEvent(Type, **data)` with a closed union — one frozen dataclass per event: `TextChunk`, `ToolCallStarted`, `ToolResult`, `Suspended(interactions, plans)`, `Checkpoint`, `Relayed(child_event)`, `Done`, `Error`, and the rest. The executor consumes it with a typed match, and an unhandled variant is a static error at every incomplete dispatch — not a silently-dropped `elif`. This turns the two files' shared-by-convention contract into a declared one, and it retires the `data.get(...)` coercion at every consumer. (`CHECKPOINT` stops being a control signal disguised as a data event: it is simply the `Checkpoint` variant, and the executor's reaction to it is explicit.)

## `TurnMachine`: control flow you can see

Lift the turn lifecycle out of `stream()`/`execute()` into an explicit state machine. The states are the phases that are today implicit in the method's shape: `Planning` (one model call), `Deciding` (preflight the batch's permissions), `Executing` (drain the approved batch), `Suspended` (a human gate), `Compacting`, and the terminals `Done`/`Failed`/`Canceled`. Each state is a handler that takes the turn context and yields `TurnEvent`s plus the next state; the driver is a small loop that runs handlers until a terminal state. `return`/`continue`/`yield` woven through a mega-method become named transitions.

Two things fall out of this that the durable-turn work reached for and missed. First, **suspension and resume are the same state, entered twice**: `Suspended` persists the `TurnRecord`'s pending interactions and stops; resume re-enters the machine at `Executing` with the answered decisions applied — identical whether the runtime was rebuilt from the checkpoint (top-level) or is still live (delegated). Second, the top-level/delegated divergence collapses into an injected **`Continuation`** strategy — `DurableSegment` (persist, close the A2A segment, rebuild later) or `InProcessPark` (await the answer in place) — chosen once at the `Suspended` transition, rather than forked through the body of two methods and a resolver. The state machine is what finally lets that unification be total instead of partial.

Because the machine's whole state is `(current_state, TurnRecord, conversation)`, each state is unit-testable in isolation, and a resumed turn is just the machine re-hydrated at `Suspended` — no special resume path to keep in sync with the main path.

## `TurnStore`: one facade, and an honestly-named `JobStore`

Stop claiming the task store is "everything." Define one `TurnStore` facade that owns a turn's durable state — the `TurnRecord`, the conversation checkpoint, and the A2A wire history — behind typed methods (`save`, `load`, `load_conversation`, `reconcile_orphans`), so there is exactly one object that reads and writes turn persistence and one place to reason about it. Rename `background_store` to `JobStore` and give it a one-sentence charter at the top of the file: *it owns background-job lifecycle and OS process-group reaping; it is not turn state.* The result is still two stores — but each name tells the truth, and nothing is a hidden third surface. "Single source of truth for a turn" becomes literally true because there is a single object that is it.

## The frontend that mirrors it

The same discipline, on the other side of the wire.

**One typed reader.** A generated discriminated union already exists (`events.ts` / `events.schema.json`); make it the *only* way parts are read. `reduceDataPart` and `reduceAgentLaneEvent` switch on the union's discriminant with an exhaustive `switch` and a `never` default, so a renamed or unknown kind is a compile error, not a `String(data.x ?? "")` that quietly yields `""`. `message.meta` gets a typed per-kind model; `ToolCall`'s ignored `permission`/`question`/`onPermission` props — vestigial dead weight — are removed, and the actionable prompt stays where it actually lives (the overlay).

**i18n every user-facing string.** Every label that today lives in a hand-written dictionary — `AGENT_STATE_LABEL_KEY`, the tool-status labels, the question-option scaffolding — routes through next-intl. Nothing user-visible is hardcoded English inside a dict.

**The rule, stated for the frontend:** a dictionary that maps a fixed set of variants to behavior is a discriminated union in disguise (make it one); a dictionary of user-facing strings is missing i18n (route it through next-intl). A dict survives only as a genuinely open, transient map.

## The discipline that keeps entropy out

One invariant, enforced, is what prevents the slow return of the mess: **no untyped dict at a boundary.** Persisted shapes are pydantic models; wire contracts are typed unions; rendered labels are i18n keys; the runtime↔executor and client↔server contracts are declared types, not conventions. A dict is permitted only as a local computation that never escapes its function. A lightweight CI guard (an AST check for `.get(` on `metadata`/event payloads, and an ESLint rule against `Record<string, unknown>` at the reducer's read sites) makes the rule mechanical rather than aspirational.

## Build order

This is a refactor, so it must land in small, compiling, behavior-preserving steps — and the god-method split comes *after* the typed core exists to split into, never as a big-bang rewrite.

1. **`TurnRecord`.** Introduce it and route every `task.metadata` turn-state access through it; delete the raw-key pokes. Behavior-identical; the serialized bytes match the old keys until a later step is free to reshape them.
2. **`TurnEvent`.** Introduce the typed union behind the existing `StreamEvent` surface, migrate producers and consumers to it, and delete the `**data` bag and the `data.get(...)` reads.
3. **`TurnStore` + `JobStore`.** Wrap the current tables in the facade, migrate call sites, and rename/charter the background store.
4. **`TurnMachine`.** Extract states out of `stream()`/`execute()` one at a time — `Planning`, `Deciding`, `Executing`, `Suspended` — each already speaking `TurnEvent`, until the two methods are thin drivers and the `Continuation` strategy replaces the top-level/delegated fork. This is the largest step and the reason the first three exist: it is safe only once the record, the events, and the store are typed.
5. **Frontend.** Adopt the generated union at the read sites with exhaustive switches; type `message.meta`; remove the vestigial `ToolCall` props; i18n every label dictionary.
6. **Guardrails.** Add the "no untyped dict at a boundary" checks (Python AST guard + ESLint rule) to CI.

Each step is independently valuable and independently revertible; the codebase is more legible after step 1 and never less legible in between.

## Testing

Every step is behavior-preserving, so the bar is "the same observable behavior, with fewer ways to be wrong." The fake-LLM turn harness proven out in the durable-turn work is the regression net: it drives the real runtime and executor through suspend/resume and tool execution, and each refactor step must leave its transcripts unchanged. Additionally: `TurnRecord` round-trips to and from the exact task metadata the old keys produced (until a step deliberately reshapes it); the `TurnEvent` union reproduces the current `StreamEvent` sequence for the same scripted model; and each `TurnMachine` state is unit-tested in isolation now that it is a pure `(context) -> (events, next_state)` step. On the frontend, the exhaustive switches make whole classes of "unknown kind silently dropped" bugs unrepresentable, which is its own test.

## Open questions

- Whether a `TurnMachine` state should be a coroutine/generator (keeps streaming ergonomics, but re-tangles control flow) or a pure `(context) -> (events, next_state)` step with a thin adapter that yields (maximally testable). Recommend the pure step.
- Whether the backend `TurnEvent` union and the frontend event union should be code-generated from one shared schema, making the wire contract single-sourced — appealing, but it couples the Python and TypeScript build systems; defer unless the contract drifts.
- Whether `JobStore` should eventually move under `TurnStore`'s engine (one database, two logical stores) for operational simplicity, or stay a separate file/db; the charter matters more than the file boundary.
- How far to push the "no untyped dict" rule into the A2A `DataPart.data` payloads, which are dict by the spec — the answer is a typed parse at the boundary (validate the dict into a model on the way in, never read it raw downstream), not a fight with the wire format.
