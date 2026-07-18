# The Unified Durable Turn

This plan collapses the two mechanisms Daisy grew for one idea — a turn that pauses and resumes — into a single durable primitive. [`input-required.md`](./input-required.md) made a *top-level* turn durable: it suspends as a segment, persists a checkpoint, and resumes from the database across a restart. [`delegated-agent-permissions.md`](./delegated-agent-permissions.md) made a *delegated* turn pause too, but by a different route — it parks on an in-memory future and resumes in place, ephemerally. Two pause models, two persistence stories, one concept. Here, every turn — a user turn, an autonomous wake, a compaction, and a delegated turn alike — becomes one instance of the same durable state machine, keyed to its A2A task, with the task as the single source of truth. There is no backward-compatibility constraint: the parked-future path, the context-keyed `conversations` table, and the standalone background-job store are removed, not kept beside the new model. It builds on the findings in [`audit.md`](../audit.md).

## Where we are today

One concept wears two implementations, and its durable state is scattered across four surfaces.

A **top-level turn** suspends durably. Its preflight resolves the whole tool batch, appends the tool-call `AIMessage` checkpoint, emits `SUSPENDED`, and returns; the executor writes the pending interactions into the task's `metadata`, saves the conversation through the context-keyed path, and drives the task to `input-required` with `final=True`, closing the segment cleanly. A later answer — an external `input_response` `message/send` or a native resolve — rebuilds the runtime from the persisted conversation and drives `resume_stream` as the next segment. This survives a restart, because the checkpoint is in the database.

A **delegated turn** pauses differently. Its preflight raises the same gates, but instead of closing a segment the runtime parks the turn on an in-memory `asyncio.Future` in `_agent_permission_futures`, emitting `PERMISSION_REQUEST`/`QUESTION` events that the parent relays into the agents panel. The answer resolves the future in place and the same continuous delegate stream carries the resumed work. Nothing is persisted; a restart fails the paused task, because the future — and the parent that consumes the stream — are gone.

And the durable state of a turn has no single home. The LangChain checkpoint lives in the context-keyed `conversations` table (`app.py`, `_save_conversation`/`_load_conversation`). The pending-interaction record lives in the A2A task's `metadata` (`a2a_executor.py`, `PENDING_INTERACTION_KEY`). Completed-but-undelivered background results live in the background-job store (`_replay_stored_background_results`, `undelivered_jobs`). A delegated turn's live pause lives only in the in-memory `_agent_permission_futures`. Nothing answers "what is the durable state of this turn" in one place — and the split is not academic. This session's delegated-gate work had to special-case the awaiting-input marker, re-derive the approval overlay's pending prompt from a second source (the agents-panel steps, not just the transcript), and reason about restart separately for each model. Each was a symptom of the same concept implemented twice.

## The core idea

A turn is one durable state machine, and its entire cross-restart state is one record carried on its A2A task. Every turn is an instance of that primitive; the differences that used to justify separate implementations become fields on the record, not forks in the code.

- **The A2A task is the turn's durable record.** Its store holds, per task, the internal checkpoint (the running conversation as LangChain messages) and the small control state — pending interactions, resolved decisions, turn kind, restart policy, and the inbox of delivered background results — alongside the A2A-visible history/artifacts/status. One store, keyed by task id; the task is the source of truth.
- **Checkpoint at every safe point, not only at a pause.** The turn writes its state at each point where the conversation is valid to resume from — after the model's `AIMessage`, after each tool's `ToolMessage`, at a compaction boundary, at a human pause. A crash resumes from the last durably-written safe point, not only from a human decision.
- **One suspend, one resume.** Any turn that needs a human decision writes its pending interactions to the task and suspends its segment; any answer rebuilds the runtime from the task's durable record and drives the next segment. The delegated turn stops parking on a future and resumes from its record exactly as a top-level turn does.
- **Restart is a policy on the record, not a second code path.** On reload, the reconciliation reads each non-terminal task's turn kind: a top-level pause resumes, a delegated pause fails (its parent is gone), and any turn caught mid-execution fails (its in-flight tools did not complete). One pass, driven by data.
- **Background work is results-durable, execution-ephemeral.** Detached tools and spawned agents that outlive their turn persist their completed results onto the task and replay them into the next turn; their in-flight execution does not survive a restart. There is no durable job queue.

## The turn record

The durable record is co-located with the A2A task and written through the existing `AppendOnlyTaskStore`, whose append-only, O(delta) history rows already give cheap incremental writes. It has two parts, split by how they grow.

The **checkpoint** is the turn's LangChain conversation — messages with `tool_calls` and `ToolMessage`s intact — stored as an append-only internal message log keyed by task id, distinct from the A2A wire `history` (which stays the external view; the two formats are not losslessly interconvertible, so the internal log is authoritative for resume and the wire history for clients). The running dialogue of a context is no longer a separate context-keyed table: it is the ordered concatenation of the context's tasks' logs, since A2A already groups a session's turns as related tasks under one `context_id`. Rebuilding a turn's conversation means loading the context's prior tasks' logs plus this task's log up to the checkpoint — the same `messages_from_dict` round-trip used today, now sourced from the task store rather than the `conversations` table.

The **control state** is small and lives in the task's `metadata`: the pending interactions (gates, preflight plans, and recorded answers), the resolved decisions for the batch in flight, the turn kind (`user` | `autonomous` | `compaction` | `delegated`), the restart policy that follows from it, and the background-result inbox (completed detached results not yet folded into the conversation). Because it is small, rewriting it per safe point is negligible; the large part — the conversation — only ever appends.

This is the single source of truth. `_conversations` (the process-wide in-memory dialogue map) is demoted from authority to a write-through cache of the task-store checkpoint; the context-keyed `conversations` table, the `PENDING_INTERACTION_KEY` metadata as a distinct concept, the background-job store, and `_agent_permission_futures` are gone.

## Checkpointing and crash-consistency

A turn advances through safe points, and durability rides them. When the runtime appends the model's `AIMessage` (before any tool runs), when it appends a tool's `ToolMessage`, when it finishes a compaction, and when it suspends for a human, it writes the checkpoint delta and the current control state to the task. The write is a precondition for proceeding: a tool's side effect is only allowed to happen after the decision to run it — and the checkpoint that records the batch — is durable.

Resume is **at-most-once**, which the existing design already makes safe. Permissions are resolved for the whole batch in preflight, before any side effect, so a resume never re-decides. A tool that started but whose `ToolMessage` was not durably written before a crash is treated as interrupted on resume — it records the `(interrupted)` result the runtime already synthesizes for an aborted tool, and is never re-run. The model sees the interruption in the conversation and chooses whether to retry, exactly as it does for a user-Stop today. No write, spawn, or egress is silently repeated. The cost — a tool that completed its side effect but crashed before recording its result is reported as interrupted, so its effect happened without a recorded success — is accepted: it is rare (the window is one durable write), it is the safe direction (the model re-checks rather than assuming), and the alternative (at-least-once) would demand a per-tool idempotency key for every side effect, which is disproportionate for a local-first, single-user harness.

## Suspend and resume, unified — including delegation

Suspension is one mechanism for every turn. The runtime's preflight (already the shared decision path) yields the pending interactions; the executor writes them to the task's control state and closes the segment, and any answer rebuilds the runtime from the task record and drives `resume_stream` as the next segment. What used to distinguish top-level from delegated collapses to two data points: **who delivers the answer**, and the **restart policy**.

For a **top-level** turn the segment closes as A2A `input-required` with `final=True` — the spec-visible pause an external client answers with an `input_response` `message/send`, or a native REST resolve answers in-band. The resume rebuilds from the database. Restart policy: preserve.

For a **delegated** turn the same suspend happens — the child writes its record and ends its segment at the pause — but the answer is delivered by the parent's delegation driver rather than an external client. The parent's `_run_spawned_agent` no longer consumes one continuous parked stream; it consumes segments. When the child's segment closes at `input-required`, the driver sees the pause (not an end of work), relays the child's prompt into the parent's panel and the shared approval overlay (as it already does), and — when the user answers — re-invokes the child with an `input_response` referencing the child's task, driving the next segment. The child rebuilds from its own durable record and continues, using the *same* resume code as a top-level turn; the parent simply consumes the continuation stream. The in-memory future is gone: a delegated pause is now a durable segment whose continuation the parent triggers.

The restart policy is where the delegated turn stays ephemeral, per the decision that a delegation tree is not resurrected. The child's record is durable enough to resume *within a live process*, but its resume depends on the parent's in-process driver, and the parent — its model context, its background bookkeeping, its live consumption — is not restored across a restart. So a delegated `input-required` task is failed on reload, not preserved: the user re-runs the turn, which re-spawns the child. Unification buys the single suspend/resume/checkpoint path; it deliberately does not buy cross-restart survival for delegated work, whose value (versus a cheap re-run) does not justify durably reconstructing a whole delegation tree and re-attaching resumed children to a rebuilt parent.

## Restart reconciliation

On startup, one pass over non-terminal tasks reads the turn record and applies the policy the record already carries, replacing the current `fail_orphaned_tasks` special-casing with a data-driven decision:

- A **top-level `input-required`** task is preserved — its durable checkpoint and pending interactions resume it on the next answer.
- A **delegated `input-required`** task is failed — its parent driver is gone.
- Any task **caught mid-execution** (running, not suspended) is failed — at-most-once means its in-flight tools did not complete, and there is nothing safe to resume into; the turn's partial checkpoint remains as history and the user re-runs.
- **Undelivered background results** on any task's inbox are preserved and replayed into the next turn of their context.

Because every one of these is a field read from the record rather than an inference, the reconciliation is a single loop with no per-turn-kind branches beyond the policy lookup.

## Background and detached work

A detached tool or a spawned agent that outlives its originating turn is results-durable and execution-ephemeral. When it completes, its result is written to the task's background-result inbox (the durable successor to the background-job store), and the next turn of that context folds undelivered inbox entries into the conversation before the model runs — the same replay `_replay_stored_background_results` performs today, now sourced from the task record. Its *execution* is an in-process job with no durable continuation: a restart fails an in-flight detached job like any mid-execution turn, and the user re-triggers it. This keeps the harness single-process and local-first; a durable, resumable background queue is explicitly out of scope (and is the boundary at which an external durable-execution layer would earn its place — see the open questions).

## What is deleted

No compatibility shim; the superseded surfaces are removed:

- The context-keyed `conversations` table and `_save_conversation`/`_load_conversation` — the checkpoint becomes the task-keyed internal message log; `_conversations` survives only as a write-through cache.
- `_agent_permission_futures` and the parked-future branch of `stream()` — a delegated pause is a durable segment now.
- The standalone background-job store — folded into the task's background-result inbox.
- `PENDING_INTERACTION_KEY` as a separate metadata concept — folded into the one control-state record.
- The two divergent suspend paths in the executor — one `suspend(pending)` and one `resume(answer)` remain, parameterized by delivery path and restart policy.

Persisted sessions from the old stores are not migrated: the cutover is clean, and existing conversations are discarded on the switch, consistent with there being no backward-compatibility constraint.

## Security and correctness model

The invariants hold and, in places, tighten. Permission decisions are still resolved in preflight before any side effect, and are now durably recorded before the tool runs, so a resume never re-decides and never widens authority. Side effects are at-most-once: no write, spawn, or egress is repeated on resume, and a tool interrupted by a crash is reported as such rather than silently retried. Escalation stays fail-safe: a delegated pause interrupted by a restart ends as a failure, never a silent resume of orphaned work; a top-level pause that cannot be answered stays `input-required` until answered, denied, or superseded. The task is the single authorization and state boundary — there is one place a decision, a checkpoint, or a pending interaction can live, which removes the class of bug where two stores disagree about whether a turn is paused.

## Build order

1. **The turn record and store.** Add the task-keyed internal message log and the consolidated control-state block to `AppendOnlyTaskStore`; write the checkpoint and control state through it; make `_conversations` a write-through cache. Reconstruct a context's conversation as the ordered concatenation of its tasks' logs.
2. **Safe-point checkpointing.** Move the runtime's durable writes from "only at suspend" to every safe point (post-`AIMessage`, post-`ToolMessage`, post-compaction, at suspend), and formalize the at-most-once interrupted-tool handling on resume.
3. **Unified suspend/resume.** Reduce the executor to one `suspend(pending)` and one `resume(answer)` over the turn record, parameterized by delivery path and restart policy; delete the parked-future path.
4. **Delegated segments.** Rework `_run_spawned_agent` to consume delegate segments and re-invoke the child with an `input_response` on answer; relay the child's prompt to the overlay (already in place) and route the answer back through the driver.
5. **Restart reconciliation.** Replace `fail_orphaned_tasks`'s special-casing with the one data-driven pass over turn records (preserve top-level pauses, fail delegated pauses and mid-execution turns, replay undelivered results).
6. **Background inbox.** Fold the background-job store into the task's background-result inbox and its replay.
7. **Deletion pass.** Remove the `conversations` table, `_agent_permission_futures`, the standalone job store, and `PENDING_INTERACTION_KEY` as a distinct concept; clean cutover with no migration.

## Testing

The checkpoint round-trips through `messages_to_dict`/`messages_from_dict` with `tool_calls` and `ToolMessage`s intact, now sourced from the task store, and a context's conversation reconstructs correctly as the ordered concatenation of its tasks' logs. Against the real `a2a-sdk`, a top-level turn suspends as a `final=True` `input-required` segment and resumes from the record to completion, and — the new coverage — a *delegated* turn suspends as a segment and its continuation is driven by the parent re-invoking it with an `input_response`, reaching completion after an approval and ending denied after a denial, with the prompt surfaced in the shared overlay. Crash-consistency is exercised by cutting the process at each safe point and asserting the resume is at-most-once: a tool whose `ToolMessage` was not durably written is reported interrupted and never re-run, and no side effect is duplicated. A restart test confirms the data-driven reconciliation preserves a top-level pause, fails a delegated pause and any mid-execution turn, and replays an undelivered background result. A deletion test confirms nothing reads the old `conversations` table, the job store, or `_agent_permission_futures`.

## Open questions

- Whether the internal checkpoint log and the A2A wire `history` should remain two representations on the task, or whether the wire history should be *derived* from the internal log at read time (removing one stored copy at the cost of a projection on every read).
- Whether checkpointing at every safe point is worth its write volume for very tool-heavy turns, or whether a coarser cadence (every N tool results, plus every suspend) is the better default — a tunable with a safe upper bound.
- Whether a context's unbounded growth as "the concatenation of all its tasks' logs" needs a durable compaction of *old tasks* (not just in-conversation compaction), so reopening a long-lived session does not reload an ever-growing task set.
- The standing boundary: if delegated or background work must ever survive a restart and run across processes, the ephemeral-execution decision here is what an external durable-execution layer (DBOS in-process, or Temporal if genuinely distributed) would replace — deliberately out of scope while the harness is single-process and local-first.
