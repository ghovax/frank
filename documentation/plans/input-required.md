---
created: 2026-07-18T12:44:32Z
updated: 2026-07-18T19:19:31Z
commit: 7036f6c
---

# Durable, Segment-Based input-required

This is the plan for making Daisy's human-in-the-loop pauses — permission prompts and `ask_user` questions — a first-class, durable, A2A-idiomatic `input-required` flow. It supersedes the parked-coroutine approach sketched in [`external-agents.md`](./external-agents.md) under "Human-in-the-loop via input-required" and builds on the findings in [`audit.md`](./audit.md). There is no backward-compatibility constraint: the parked-coroutine model is removed rather than kept alongside.

## Where we are today

When the runtime needs a human decision it creates an in-memory `asyncio.Future`, keyed `perm-{context}-…` / `q-{context}-…`, stores it in a process-global dict, yields a `PERMISSION_REQUEST` or `QUESTION` event, and then awaits that future deep inside the concurrent tool batch (`agent.py`, in `_execute_tool`). The A2A executor turns that event into an `input-required` task status and keeps the whole turn coroutine parked on the future while the connection stays open (`a2a_executor.py`, `emit_input_required`). A native REST resolve endpoint or an external `input_response` message sets the future's result, and the parked coroutine wakes and finishes the turn.

This has two disqualifying problems. First, it is not durable: the future and the mid-turn runtime state live only in memory, and `AppendOnlyTaskStore.fail_orphaned_tasks` (`task_store.py:282`) fails every non-terminal task on restart, so a paused task is destroyed when the server bounces. Second, it fights the A2A SDK's queue lifecycle. The SDK models each `message/send` as one producer that owns the task's event queue and closes it (`_cleanup_producer` → `queue_manager.close(task_id)`) when it finishes. Daisy runs the entire turn — including everything after the pause — inside the first producer while parking it, so when a spec-compliant external client answers by referencing the same task id, that second producer's completion tears down the still-in-use queue. The observable result, reproduced against the real `a2a-sdk`, is that the resumed output is silently dropped ("Queue is closed"), the streaming client is stranded at `input-required`, and the task is persisted as `completed` with no artifact.

## The core idea

Adopt the model the SDK is built around: a task advances in **segments**, one per `message/send`. A call streams up to the next pause or terminal state and ends; the answer is a new call that drives the continuation. Concretely:

- `input-required` is emitted as a `final=True` status update, so the producing call's stream closes cleanly at the pause and the queue is released. No coroutine is parked, and no second producer can tear down a queue the first is still using. (The SDK's consumer already treats a `final` status update as stream-terminal; `input-required` on a bare `TaskStatusUpdateEvent` with `final=False` is exactly what forced the parked-coroutine workaround.)
- The turn's cross-segment state is durable in the database, not in memory. The resume checkpoint is a natural, already-serializable object: an `AIMessage` that carries `tool_calls` with no following `ToolMessage`s. LangChain's `messages_to_dict` / `messages_from_dict` — which Daisy already uses for conversation persistence (`app.py`, `_save_conversation` / `_load_conversation`) — round-trips `tool_calls` and `ToolMessage`s losslessly, so the mid-turn conversation persists faithfully through the existing `conversations` table.
- The answer rebuilds the runtime from that persisted conversation and drives the next segment. Because the checkpoint is durable, this survives a restart: a reloaded `input-required` task resumes from the database rather than being failed.

## Preflight: deciding the whole batch before any tool runs

The model emits several tool calls in one response and Daisy runs them concurrently (`_drain_tools_concurrently`). Today each tool gates its own permission mid-execution. That cannot be checkpointed: if one tool pauses for a decision while its siblings have already started, a resume would either lose their side effects or double-run them. So the decision model changes — permissions for the entire batch are resolved **before any tool executes**.

A preflight pass classifies each tool call into one of three outcomes: auto-approved, hard-denied (a policy block, with the exact error and any denial injection the tool would have produced), or needs-human. It reuses the existing decision logic verbatim — `_evaluate_bash_permission`, `_classify_permission`, `tools.bash.read_only_assessment`, `_outside_working_directory_reads`, `_command_session_allowed`, and `_is_remote_agent` — so a refactor cannot silently widen what is auto-approved. The gates it consolidates are the bash sandbox-read and risk approvals, the MCP risk approval, the spawn/`call_remote_agent` egress consent, and the `ask_user` question (whose "decision" is simply the answers). A single tool can present more than one gate — a command that both reads outside the working directory and is high-risk — so each gate is its own pending interaction tied to the tool-call id, and the tool runs only if every one of its gates is approved.

If the batch has no pending gates, execution is unchanged: tools run concurrently with their decisions already in hand, and `_execute_tool` no longer prompts — it is told the decision. If any gate needs a human, the runtime appends the tool-call `AIMessage` (the checkpoint), emits a single `SUSPENDED` event carrying every pending interaction, and returns. Execution has not started, so there is nothing to unwind.

## Runtime shape

`AgentRuntime.stream` gains a sibling, `resume_stream(decisions)`. Both drive the same turn loop; `resume_stream` enters it with the pending `AIMessage` already at the tail of the conversation, applies the decisions to that batch, appends the resulting `ToolMessage`s, and then continues normally into the next model call. The new `StreamEvent.Type.SUSPENDED` is the only addition to the event vocabulary; `PERMISSION_REQUEST` and `QUESTION` remain the per-interaction descriptors carried inside it, so the native client's rendering is unchanged. The process-global `_pending_permissions` / `_pending_questions` dicts are demoted from source of truth to a transient per-process cache; the durable truth is the pending-interaction record.

## Executor and persistence

`HarnessAgentExecutor.execute` drives one segment. On `SUSPENDED` it writes the pending-interaction record into the task's `metadata` (persisted losslessly by `AppendOnlyTaskStore`, co-located with the `input-required` task), the conversation checkpoint is saved through the existing per-context path, it emits `input-required` with `final=True`, and it returns — the segment ends and the queue closes cleanly. On a terminal event it finalizes as today.

An answer — whether an external `input_response` `message/send` referencing the task, or a native resolve — records its decision against the pending interaction. When every interaction for the task is answered, the executor rebuilds the runtime from the persisted conversation and drives `resume_stream(decisions)` as the next segment on that call's own queue, streaming to completion or the next pause. Both answer paths converge on this one durable resume, so there is a single source of truth and a single code path.

Restart survival falls out of the same state. `fail_orphaned_tasks` is changed to exempt `input-required`: such a task is left intact, and its next answer rebuilds and resumes it from the database. Every other non-terminal state is still failed on restart, because only `input-required` has a durable, resumable checkpoint.

## Superseding a pause, and delegated agents

Because a suspended segment releases the per-context turn lock (nothing is parked), a new user message can arrive while a task is still input-required. Rather than answer, it supersedes the pause: the runtime closes the dangling checkpoint (an `AIMessage` with tool_calls and no `ToolMessage`s) by appending a "superseded" `ToolMessage` for each call, so the conversation stays valid, and the executor drops the awaiting-input marker. A late answer for the superseded pause then finds no checkpoint and is a no-op.

Delegated agents do not durably suspend. A delegated turn is a fresh, one-shot run whose throwaway conversation is not persisted, so it has no resumable checkpoint — and an autonomous agent has no reliable interactive human anyway. A gate that would prompt is therefore turned into a hard denial for an agent (as the sandbox gate already was), so a delegated agent runs read-only or the parent performs the guarded action. Interactive approval is for the top-level user turn.

> **Superseded.** The hard-denial decision in the paragraph above was later reversed by [`delegated-agent-permissions.md`](./delegated-agent-permissions.md). A delegated gate is no longer denied: the delegated agent *parks in place* on an in-memory future (staying a `working` task, not durably suspending), emits the prompt to the agents panel and the shared overlay, and resumes on the user's answer. It remains true that a delegated turn does not durably suspend and is failed on restart rather than preserved — only the "hard-deny instead of prompt" conclusion changed.

## Security model

The one security-sensitive move is relocating permission decisions from execution time to preflight. The mitigation is that the decision logic is moved verbatim and continues to flow through the same shared functions, so the set of things that auto-approve, hard-deny, or prompt is unchanged — only the timing moves earlier. `_execute_tool` becomes strictly less privileged: it can no longer approve anything, only carry out a decision made upstream or emit the denial the preflight recorded. Bypass mode, read-only enforcement, and explicit deny rules keep their current force, since they are computed by the same functions.

## Build order

1. Runtime: add `SUSPENDED`; extract the per-tool permission plans and `_preflight_permissions`; route the tool batch through preflight; add `resume_stream`; strip inline gating from `_execute_tool` so it consumes a resolved decision.
2. Persistence: the durable pending-interaction record in task metadata, and exempting `input-required` from `fail_orphaned_tasks`.
3. Executor: the segment model — `input-required` as `final=True`, checkpoint persistence, and rebuild-and-resume.
4. Wiring: converge the native REST resolve and the external `input_response` answer on the one durable resume, and reload paused tasks on startup.

## Testing

Full verification requires driving a real model turn, which is a project-level concern. What is verified in isolation: the checkpoint round-trips through `messages_to_dict` / `messages_from_dict` with `tool_calls` and `ToolMessage`s intact; the segment and resume mechanics behave correctly against the real `a2a-sdk` `DefaultRequestHandler` / `InMemoryQueueManager` / `TaskStore` (a `final=True` `input-required` closes the first segment, and a task-referencing answer resumes to completion without dropping events); and the preflight classifies representative approve, hard-deny, and needs-human tool calls the same way the inline gates did.

## Open questions

- Whether multiple pending interactions in one batch should surface as one combined prompt or several; the current plan keeps them separate, one per gate, and resumes only when all are answered.
- Whether the pending-interaction record belongs in task metadata (co-located, chosen here) or in a dedicated table alongside the conversation checkpoint.
- Whether a paused task should expire after some interval so an abandoned prompt does not hold its context indefinitely.
