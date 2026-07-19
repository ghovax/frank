# The Unified Durable Turn

This plan collapses the two mechanisms Daisy grew for one idea — a turn that pauses and resumes — into a single durable primitive. [`input-required.md`](./input-required.md) made a *top-level* turn durable: it suspends as a segment, persists a checkpoint, and resumes from the database across a restart. [`delegated-agent-permissions.md`](./delegated-agent-permissions.md) made a *delegated* turn pause too, but by a different route — it parks on an in-memory future and resumes in place, ephemerally. Two pause models, two persistence stories, one concept. This plan unifies the **durable state** the two shared but stored apart: the model-facing conversation, the restart policy, and the resolve/overlay surfaces all move behind one source of truth — the A2A task store. Every turn suspends through one `SUSPENDED` event with one prompt-rendering path; a top-level turn is the durable primitive (suspend → persist checkpoint → rebuild → resume), and a delegated turn shares that event and resolver but keeps an in-process parked *continuation* — the one genuinely-necessary specialization for ephemeral in-process work, no longer a parallel event/handler/resolve stack. There is no backward-compatibility constraint: the context-keyed `conversations` table and the delegated `PERMISSION_REQUEST`/`QUESTION` event machinery are removed, not kept beside the new model. (One collapse the first draft proposed — folding `background_store` into a task inbox — was evaluated and deliberately not done, because `background_store` is a distinct subsystem, not a turn-pause duplicate; the section below says why.) It builds on the findings in [`audit.md`](../audit.md).

## Where we are today

One concept wears two implementations, and its durable state is scattered across four surfaces.

A **top-level turn** suspends durably. Its preflight resolves the whole tool batch, appends the tool-call `AIMessage` checkpoint, emits `SUSPENDED`, and returns; the executor writes the pending interactions into the task's `metadata`, saves the conversation through the context-keyed path, and drives the task to `input-required` with `final=True`, closing the segment cleanly. A later answer — an external `input_response` `message/send` or a native resolve — rebuilds the runtime from the persisted conversation and drives `resume_stream` as the next segment. This survives a restart, because the checkpoint is in the database.

A **delegated turn** pauses differently. Its preflight raises the same gates, but instead of closing a segment the runtime parks the turn on an in-memory `asyncio.Future` in `_agent_permission_futures`, emitting `PERMISSION_REQUEST`/`QUESTION` events that the parent relays into the agents panel. The answer resolves the future in place and the same continuous delegate stream carries the resumed work. Nothing is persisted; a restart fails the paused task, because the future — and the parent that consumes the stream — are gone.

And the durable state of a turn has no single home. The LangChain checkpoint lives in the context-keyed `conversations` table (`app.py`, `_save_conversation`/`_load_conversation`). The pending-interaction record lives in the A2A task's `metadata` (`a2a_executor.py`, `PENDING_INTERACTION_KEY`). Completed-but-undelivered background results live in the background-job store (`_replay_stored_background_results`, `undelivered_jobs`). A delegated turn's live pause lives only in the in-memory `_agent_permission_futures`. Nothing answers "what is the durable state of this turn" in one place — and the split is not academic. This session's delegated-gate work had to special-case the awaiting-input marker, re-derive the approval overlay's pending prompt from a second source (the agents-panel steps, not just the transcript), and reason about restart separately for each model. Each was a symptom of the same concept implemented twice.

## The core idea

A turn's durable state is one record carried on its A2A task, and the restart policy is a field on that record rather than a fork in the code. The model-facing conversation, the pending interactions, and the turn kind live in one place — the task store — so there is a single answer to "what is the durable state of this turn," and reopening or restarting reads it rather than reconstructing it from scattered stores.

- **The A2A task is the turn's durable record.** Its store holds, per task, the internal checkpoint (the running conversation as LangChain messages) and the small control state — pending interactions, resolved decisions, turn kind, restart policy, and the inbox of delivered background results — alongside the A2A-visible history/artifacts/status. One store, keyed by task id; the task is the source of truth.
- **Checkpoint at every safe point, not only at a pause.** The turn writes its state at each point where the conversation is valid to resume from — after the model's `AIMessage`, after each tool's `ToolMessage`, at a compaction boundary, at a human pause. A crash resumes from the last durably-written safe point, not only from a human decision.
- **One durable checkpoint, one resume path for the durable case.** A top-level turn that needs a human decision writes its pending interactions to the task and suspends its segment (`input-required`, `final=True`); the answer rebuilds the runtime from the task's checkpoint and drives the next segment. A delegated turn keeps its in-process parked continuation — see the delegation section for why that specialization is correct, not a shortcut.
- **Restart is a policy on the record, not a second code path.** On reload, the reconciliation reads each non-terminal task's turn kind: a top-level pause is preserved, a delegated pause is failed (its parent is gone), and any turn caught mid-execution is failed (at-most-once — its in-flight tools did not complete). One pass, driven by data.
- **Background work is results-durable, execution-ephemeral.** Detached tools and spawned agents that outlive their turn persist their completed results and replay them into the next turn; their in-flight execution does not survive a restart. This is what `background_store` already does, so it is kept rather than reimplemented.

## The turn record

The durable record lives in the `AppendOnlyTaskStore` — the single durable turn surface. It has two parts, split by how they change.

The **checkpoint** is the model-facing LangChain conversation — messages with `tool_calls` and `ToolMessage`s intact — stored in a `turn_checkpoint` table as one whole snapshot per context (the `messages_to_dict` form), distinct from the A2A wire `history` (the external view; the two formats are not losslessly interconvertible, so the snapshot is authoritative for resume and the wire history for clients). It is a whole snapshot, not a per-turn append-only log: the running dialogue accumulates across a session's turns *and compaction rewrites it in place* (summarizing earlier turns), and a per-turn append-only concatenation cannot represent an in-place rewrite — a whole snapshot is the only correct representation. (This corrects the plan's first sketch, which described per-task logs concatenated by a global row id; compaction breaks that.) It is written only at safe points, a few times per turn — never per stream event — so the whole-row write is cheap relative to the turn, and it is kept off the write-hot task head. `messages_from_dict` rehydrates it on resume, sourced from the task store rather than the deleted `conversations` table.

The **control state** is small and lives in the task's head `metadata`: the pending interactions (gates, preflight plans, and recorded answers) and the turn kind (`user` | `autonomous` | `compaction` | `delegated`), whose restart policy the reconciliation reads. It is written with the head, which the SDK already upserts per event, so it costs nothing extra.

`_conversations` (the process-wide in-memory dialogue map) is demoted from authority to a write-through cache of the checkpoint; the context-keyed `conversations` table and its `_load`/`_save_conversation` callbacks are deleted.

## Checkpointing and crash-consistency

A turn advances through safe points, and durability rides them. Right after each tool-result batch is appended — the point where the conversation carries no dangling `tool_call` — the runtime emits a `CHECKPOINT` stream event, and the executor (which owns persistence) snapshots the conversation. The runtime only marks the safe point; it stays free of the store. Compaction and suspension are additional safe points that write through the same path. The value is concrete: a crash mid-turn is *failed*, not resumed, so the only thing safe-point checkpointing buys is that the record of already-completed, side-effecting tools survives into the next turn — so the model sees them and does not redo them, which is the practical face of the at-most-once guarantee.

Resume is **at-most-once**, which the existing design already makes safe. Permissions are resolved for the whole batch in preflight, before any side effect, so a resume never re-decides. A tool that started but whose `ToolMessage` was not durably written before a crash is treated as interrupted on resume — it records the `(interrupted)` result the runtime already synthesizes for an aborted tool, and is never re-run. The model sees the interruption in the conversation and chooses whether to retry, exactly as it does for a user-Stop today. No write, spawn, or egress is silently repeated. The cost — a tool that completed its side effect but crashed before recording its result is reported as interrupted, so its effect happened without a recorded success — is accepted: it is rare (the window is one durable write), it is the safe direction (the model re-checks rather than assuming), and the alternative (at-least-once) would demand a per-tool idempotency key for every side effect, which is disproportionate for a local-first, single-user harness.

## Suspend and resume — one event, two continuations

**One suspend event.** A turn that needs a human decision — top-level or delegated — yields a single `SUSPENDED` event carrying the pending interactions and the preflight plans. The executor renders the prompt from it the same way for both (the `permission_request` / `question` DataParts — shown in the transcript for a top-level turn, relayed to the agents panel for a delegated one). There is no separate delegated event vocabulary: the old per-gate `PERMISSION_REQUEST` / `QUESTION` stream events and their duplicate executor handlers are deleted.

**One thing differs — the continuation transport — in one clearly delineated place.** After `SUSPENDED`:

- A **top-level** turn returns. The executor writes the pending interactions to the task's control state, closes the segment as A2A `input-required` with `final=True`, and the answer — an external `input_response` `message/send` or a native REST resolve — rebuilds the runtime from the task's checkpoint and drives `resume_stream` as the next segment. Restart policy: preserve.
- A **delegated** turn parks in place (`_await_pending_answers`) and continues the *same* stream once answered. This is not the old parallel machinery — it is the tail of the one suspend path — but it stays an in-process, ephemeral continuation rather than a durable segment, and that is necessary, not a shortcut:
  - **A durable segment would buy nothing.** A delegated pause is ephemeral on restart (the decision in [`delegated-agent-permissions.md`](./delegated-agent-permissions.md)): the parent's in-process driver is gone after a restart, so the child is failed regardless. A durable checkpoint written only to be discarded on reload is pure waste.
  - **The reason segments exist does not apply.** Segments were forced for top-level turns because a spec-compliant external client answers with a *new* `message/send`, and parking the coroutine across that let the SDK tear down the still-in-use queue. A delegated turn's "client" is the parent's in-process driver consuming the delegate stream; parking the child coroutine keeps its queue producer alive (verified against the real SDK), so there is no queue-teardown to avoid.

So the pause is unified in its event, its prompt rendering, its resolver, and its overlay; the delegated *continuation* remains an in-process park (`_agent_permission_futures`, completed through the shared resolver's delegated path) — the one genuinely-necessary specialization for ephemeral in-process work, not a second implementation of the same thing.

## Restart reconciliation

On startup, `reconcile_orphaned_turns` (replacing `fail_orphaned_tasks`) makes one pass over non-terminal tasks and reads the stamped turn kind:

- A **top-level `input-required`** task is preserved — its durable checkpoint and pending interactions resume it on the next answer. (An unmarked pause is treated as top-level, so the policy is safe by default.)
- A **delegated `input-required`** task is failed — its parent driver is gone. In the parked model a delegated turn does not actually reach `input-required`, so this branch is a correct, future-proof guard rather than a hot path.
- Any task **caught mid-execution** is failed — at-most-once means its in-flight tools did not complete; the turn's partial checkpoint remains as history and the user re-runs.

Every decision is a field read from the record, so the reconciliation is one loop with a single policy lookup. Background results are delivered separately by `background_store`'s existing startup replay.

## Background and detached work

A detached tool or a spawned agent that outlives its originating turn is results-durable and execution-ephemeral — and this is exactly what `background_store` already provides: a completed result is persisted (`STATUS_COMPLETED`) and replayed into the next turn on startup (`_replay_stored_background_results`), while a job still running at a crash is recovered if idempotent or abandoned-with-a-note otherwise. So the plan's original "fold the background store into a task inbox" is dropped: `background_store` additionally reaps orphaned OS process groups and recovers running jobs — capabilities a task-metadata inbox does not carry — and its behavior is already the spec's, so consolidating storage would only trade a stronger durable component for a weaker one. A durable, resumable background *queue* remains out of scope (the boundary at which an external durable-execution layer would earn its place — see the open questions).

## What is deleted

No compatibility shim; the superseded surfaces are removed:

- The context-keyed `conversations` table and `_save_conversation`/`_load_conversation` — the checkpoint is the task store's `turn_checkpoint`; `_conversations` survives only as a write-through cache. Persisted conversations from the old table are not migrated — a clean cutover, consistent with there being no backward-compatibility constraint.
- The `PERMISSION_REQUEST` and `QUESTION` stream events, their per-gate emission in `stream()`, and their duplicate executor handlers — replaced by the single `SUSPENDED` event and its one prompt-rendering path.

Deliberately **kept** as necessary, not-superseded machinery (see their sections): `background_store` (a distinct job-lifecycle + OS-reaping subsystem, not a turn-pause duplicate); the delegated in-process park (`_agent_permission_futures` / `resolve_agent_permission`), which is the ephemeral continuation tail of the one suspend path; and `PENDING_INTERACTION_KEY` (the top-level pending-interaction record, already on the task).

## Security and correctness model

The invariants hold and, in places, tighten. Permission decisions are still resolved in preflight before any side effect, so a resume never re-decides and never widens authority. Side effects are at-most-once: no write, spawn, or egress is repeated on resume, and a tool interrupted by a crash is reported as such rather than silently retried. Escalation stays fail-safe: a delegated pause interrupted by a restart ends as a failure, never a silent resume of orphaned work; a top-level pause that cannot be answered stays `input-required` until answered, denied, or superseded. For a top-level turn the task is now the single state surface — one place a checkpoint and its pending interactions live — which removes the class of bug where two stores disagree about whether a turn is paused. A delegated turn's pending gate remains an in-memory future (its continuation is in-process and ephemeral), but its resolution and display already route through the shared resolver and overlay, so the user-facing state has one source even though the delegated continuation does not.

## Build order

1. **The checkpoint store.** Add `turn_checkpoint` (context-keyed conversation snapshot) plus `save_checkpoint`/`load_checkpoint`, `reconcile_orphaned_turns`, and `delete_context` to `AppendOnlyTaskStore`.
2. **Rewire conversation persistence.** Point `_runtime_for`'s load and the safe-point/end-of-turn saves at the checkpoint; make `_conversations` a write-through cache; the durable `input-required` resume rebuilds through the same path.
3. **Safe-point checkpointing.** Emit `CHECKPOINT` from the runtime after each tool-result batch; the executor snapshots on it. (The at-most-once interrupted-tool repair already exists via `_close_dangling_tool_calls`.)
4. **Reconciliation + turn kind.** Stamp the turn kind on the task; `reconcile_orphaned_turns` preserves top-level pauses and fails delegated pauses and mid-execution turns.
5. **Deletion pass.** Remove the `conversations` table, its callbacks, and the executor's load/save injection; session delete drops the context through `delete_context`.
6. **Suspend unification.** Collapse the delegated `PERMISSION_REQUEST`/`QUESTION` events into the one `SUSPENDED` event and prompt path; factor the delegated park into `_await_pending_answers`; delete the duplicate executor handlers.

The delegated in-process continuation and `background_store` are kept, for the reasons in their sections.

## Implementation status

Implemented and committed as coherent, compiling increments: the checkpoint store; the conversation rewire onto it; safe-point `CHECKPOINT`; the data-driven reconciliation with turn-kind stamping; the `conversations`-table deletion; and the `SUSPENDED` unification (one suspend event and prompt path for every turn, deleting the `PERMISSION_REQUEST`/`QUESTION` machinery). The background-store fold from the original draft was evaluated and deliberately not done (a distinct subsystem, not a duplicate).

During implementation every path was exercised against the real code with pytest, a scripted fake LLM standing in for the model (no network), and confirmed to pass:

- **Store** (real in-memory SQLite): checkpoint save/load and compaction-replace; the reconciliation policy matrix (top-level and unmarked preserved; delegated and mid-execution failed; terminal untouched); context deletion.
- **Runtime suspend/resume**: a top-level turn hits an ask-by-default gate, yields the one `SUSPENDED` event and returns with the pending tool-call checkpoint; resuming `allow` runs the tool, fires `CHECKPOINT`, and completes; resuming `deny` records a valid denial ToolMessage; and a delegated turn emits the *same* single `SUSPENDED`, parks in place, and continues the *same* stream to completion once the shared resolver answers it.
- **Executor ↔ a2a-sdk**: the real `HarnessAgentExecutor` driven through the real `DefaultRequestHandler` + `AppendOnlyTaskStore`. A top-level turn suspends as a durable `input-required` task with its pending record and conversation checkpoint persisted, and an `input_response` `message/send` rebuilds from the checkpoint and resumes to completion, clearing the pending record. A delegated turn takes the executor's delegated branch (parks in place, never closes an `input-required` segment), is answered through the shared resolver's delegated routing, and continues the same run to completion.

Every path — store, runtime suspend/resume (top-level and delegated, with `CHECKPOINT`), and executor↔SDK for both the top-level durable segment and the delegated park — was exercised against the real code with only the model faked. What a live run would add is real-model behavior in place of the scripted turns and the multi-process fan-out, not any unexercised mechanism.

## Testing

The whole path is testable off-network by scripting a fake LLM in place of the model, which is how it was verified during implementation (see *Implementation status*): the checkpoint round-trips through `messages_to_dict`/`messages_from_dict` and a compaction rewrite replaces the snapshot rather than corrupting it; the reconciliation policy matrix holds against real SQLite; a top-level turn suspends as a `final=True` `input-required` segment and resumes from the checkpoint to completion through the real a2a-sdk; and a delegated turn parks and continues on the same run. The one thing a scripted model cannot stand in for — and that a live run should still confirm — is real-model behavior around a crash: cutting the process at a `CHECKPOINT` safe point and checking the next turn is at-most-once (a tool whose `ToolMessage` was not written is reported interrupted and never re-run, no side effect duplicated), and that a reopened long session rehydrates its conversation from the checkpoint.

## Open questions

- Whether the checkpoint snapshot and the A2A wire `history` should remain two representations, or whether the wire history should be *derived* from the checkpoint at read time (removing one stored copy at the cost of a projection on every read). Kept as two, since the formats are not losslessly interconvertible.
- Whether the `CHECKPOINT`-per-tool-batch cadence is worth its write volume for very tool-heavy turns, or whether a coarser cadence (every N batches, plus every suspend) is the better default — a tunable with a safe upper bound. Currently every batch.
- The standing boundary: if delegated or background work must ever survive a restart and run across processes, the ephemeral-execution decisions here are what an external durable-execution layer (DBOS in-process, or Temporal if genuinely distributed) would replace — deliberately out of scope while the harness is single-process and local-first.
