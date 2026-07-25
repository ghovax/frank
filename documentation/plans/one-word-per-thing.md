---
created: 2026-07-25T09:35:00Z
updated: 2026-07-25T10:32:00Z
commit: 7c1a8bd
---

# One Word Per Thing

The harness has accumulated several words for the same thing and, worse, one word for several things. Some of it is drift from the migration; some of it was never right. This is the sweep that fixes the vocabulary everywhere at once — wire, code, file names, prompts, documentation, and the client's interface strings — because a rename that lands in half the places is worse than the confusion it was meant to remove.

It is one commit on purpose. Two of these renames change a field that the CLI, the daemon and the desktop client all read, and there is no ordering of partial commits in which those three agree. Splitting the work into stages would buy nothing except a window in which the tree is broken in a way no reviewer could reason about.

## The three meanings of "task"

`task` currently names three unrelated things, and every one of them carries an id field spelled some variation of `task_id`.

An **A2A task** is one turn: a message arrives, a task opens, the model works, the task closes. That is the A2A protocol's word, and inside the protocol it is correct. A **task list** is the model's own to-do list, written with `set_tasks` and shown in the UI as exactly that — a list of tasks, which is what a person calls it. A **background job** is a `bash` command or a web search still running after the call that started it returned, and it hands back a `task_identifier` that is not an A2A task at all.

The cost is visible in the code that has to explain the difference. `read_task`'s docstring spends most of its length telling the model which of the three it operates on and which handles it must never be passed. That paragraph is a symptom: a tool whose description is mostly disambiguation is a tool whose name is wrong.

So: **turn**, **task**, **job**. A turn is what a message opens — `turn_id`, `read_turn`, `TurnStore`. A task stays the to-do list, because that is what the user sees and the industry word for it. A job is background work, with a `job_id`. `Task` as a Python symbol survives only where it is literally the A2A type being constructed or parsed, which is the boundary and nowhere else.

## The verb that does not do what it says

`session.cancel` does not cancel a session. It relays `tasks/cancel` to the session's socket and aborts the turn in flight; the session keeps running and takes the next message. The frontend compounds it by calling the wrapper `abortSession`, which reads as ending the session outright. Nothing has gone wrong because of this yet, and that is luck rather than design: the method a client would reach for to stop a runaway session is the one that quietly does something else.

It becomes `turn.cancel`, and the client's wrapper becomes `cancelTurn`. `abortToolCall` keeps its name — it really does abort, and normalising a verb that is already accurate would be churn.

Two more control-plane methods are misfiled under `session.`: `session.background` lists background jobs and `session.tool_background` detaches a blocking tool call. Neither is about sessions except in the sense that everything is. They become `jobs.list` and `jobs.detach`, which is also the first use of a `jobs.` namespace the background work should have had from the start.

And `session.kill` becomes `session.end`. A session ends; a process is killed. The CLI keeps `xeac kill`, because that is the word a shell user reaches for and the CLI is allowed to be idiomatic — but the API underneath should say what it means.

## Two collections called `_watchers`

`daemon/lifecycle.py` has `self._watchers`, a dict of tasks each awaiting a worker *process* to exit. The same module's `_close_watchers` closes the *stream* subscribers of a session's event bus. They are different things, in the same file, under the same word, and a reader who has just met one will misread the other.

Meanwhile `daemon/composition.py` watches the filesystem. Three meanings, one word.

Watching becomes filesystem-only. A process is **supervised** (`_supervise`, `self._supervisors`); a stream has **subscribers** (`_close_subscribers`). The filesystem watchers keep their name because that is what the library calls it and what the operation is.

## "Peer" and "the API"

A **peer** is another session on this machine that you can address — the one that created you, or one you created. That is the whole meaning. It is not a remote agent: `cli.md` currently calls those "the registered peers" and `architecture.md` says "reach a peer on another host", which puts the word on both sides of the one distinction the two verbs exist to preserve. Remote agents are remote agents.

"The API" names three surfaces depending on the sentence: the daemon's `/rpc` control plane, the `rest/` HTTP surface the desktop client uses, and a session's own A2A socket. They get their names used — **control plane**, **GUI surface**, **session socket** — and "the API" stops being a term.

`agent` alone always means the profile. A running thing is a session. "Remote agent" stays a compound proper noun for the external case, and `send_to_remote_agent` keeps its asymmetry with `send_message` deliberately: a remote agent runs on someone else's machine at their cost, and a caller should never be unsure which side of the wire its work went to. That asymmetry is currently just a fact of the code; it becomes a stated one.

## The harness's name, and where it belongs

A key carries the harness's name for exactly one reason: it shares a dict with somebody else's keys, and the name says whose these are. Everywhere else it is noise on a field that already had a perfectly good name.

`Message.metadata` gets this right. Everything the harness adds to a message lives under `urn:xeac:ext:turn:v1`, which is the convention A2A defines for an extension, and the fields inside it are plain — `workingDirectory`, `permissionMode`, `peerSender`. The name is stated once, at the boundary, where it means "these are XEAC's attributes".

`Task.metadata` does not. Four keys sit at its top level and two of them wear a prefix: `pendingInteraction`, `referenceTurnIds`, `xeacTurnKind`, `xeacPeerSender`. That is the owner named twice in one place and not at all in the next, in the same dict, which is a convention nobody is applying. The module's own docstring admitted it and deferred the fix to a later plan; this is that plan, and the fix is small.

So `Task.metadata` gets the same shape a message already has: one `urn:xeac:ext:turn:v1` key holding the whole record, with `kind`, `peerSender`, `pending` and `referenceTurnIds` inside it. An empty record removes the key rather than leaving a husk. Nothing outside that key is touched, so the record owns exactly its own slice.

The prefix survives where it is load-bearing and nowhere else. `urn:xeac:ext:turn:v1` keeps it, because that string's whole job is to be unique against another implementation's extension. The signed-file-link audience keeps it for the same reason and changes shape: `xeac-a2a-file` becomes `urn:xeac:a2a:file:v1`. An audience is what stops a token minted for one purpose being accepted for another, so it must be unique to that purpose — strip the name and it reads `a2a-file`, exactly the string another A2A implementation would reach for. What was wrong with it was the form, not the name: RFC 7519 types `aud` as StringOrURI precisely because URIs do not collide while chosen words do, and an ad-hoc hyphenated string sat beside a URN doing the same job, so the harness had two conventions for namespaced identifiers. The version is new, so a later change to the claim set can be rejected by verifiers rather than requiring the signing secret to be rotated. The `xeac` originator and user-agent the Codex client sends keep it, because they identify this client to a provider. Log channels, socket names, the pid file and the binary keep it for the same reason — they name the program. Field names inside a namespace we already own do not.

## `context_id`

The same identifier is `session_id` in the daemon, `context_id` in the REST routes, and `contextId` on the A2A wire. The middle one is ours and has no reason to differ. It becomes `session_id`, including in the route paths the desktop client calls. `contextId` survives only where the A2A field is literally being read or written.

## The renames

### Control-plane methods

| Now | Becomes |
|---|---|
| `session.cancel` | `turn.cancel` |
| `session.kill` | `session.end` |
| `session.background` | `jobs.list` |
| `session.tool_background` | `jobs.detach` |
| `task.get` | `turn.get` |

### Wire fields

| Now | Becomes |
|---|---|
| `Task.metadata.xeacTurnKind` | `Task.metadata["urn:xeac:ext:turn:v1"].kind` |
| `Task.metadata.xeacPeerSender` | `…["urn:xeac:ext:turn:v1"].peerSender` |
| `Task.metadata.pendingInteraction` | `…["urn:xeac:ext:turn:v1"].pending` |
| `Task.metadata.referenceTurnIds` | `…["urn:xeac:ext:turn:v1"].referenceTurnIds` |
| `session.send` response `task_id` | `turn_id` |
| `session.history` response `tasks` | `turns` |
| attach `snapshot` frame `tasks` | `turns` |
| `turn.get` response `task` | `turn` |
| REST `/sessions/{context_id}/tasks` | `/sessions/{session_id}/turns` |
| terminal query parameter `context_id` | `session_id` |
| `Task.metadata.referenceTaskIds` | `referenceTurnIds` |
| background result `task_identifier` | `job_id` |

### Tools

| Now | Becomes |
|---|---|
| `read_task` | `read_turn` |
| `send_message` | `message_session` |
| `send_to_remote_agent` | `message_remote_agent` |
| `set_tasks` / `update_tasks` | unchanged — the to-do list keeps the word |

`send_message` was the one verb in the peer-session set that did not name what it operates on, beside `create_session`, `read_session`, `list_sessions` and `end_session`. Not `steer_session`: steering is already a named mechanism here — `enqueue_steering`, a `SteeringEvent` on the wire — meaning a message injected into a turn *already running*, at its next safe point. The tool also delivers to an idle session, which is not steering, so the name would put one word on two things. And steer is directional, while a peer reporting up to the session that created it is not steering it.

### Backend files

| Now | Becomes |
|---|---|
| `daemon/persistence/task_store.py` | `daemon/persistence/turn_store.py` |
| `worker/persistence.py` | `worker/turn_store.py` |
| `runtime/prompts/read_task_background_handle.md` | `runtime/prompts/read_turn_background_handle.md` |

### Persisted schema

| Now | Becomes |
|---|---|
| table `task_head` / `task_history` / `task_artifacts` | `turn_head` / `turn_history` / `turn_artifacts` |
| column `task_id` | `turn_id` |
| column `task_metadata` | `turn_metadata` |
| column `context_id` (artifact and lifecycle tables) | `session_id` |
| ingest methods `task.save` / `task.get` / `task.delete` / `turn.tasks_for_context` | `turn.save` / `turn.get` / `turn.delete` / `turn.list_for_session` |

### Backend symbols

| Now | Becomes |
|---|---|
| `_session_cancel` / `_session_kill` | `_turn_cancel` / `_session_end` |
| `_session_background` / `_session_tool_background` | `_jobs_list` / `_jobs_detach` |
| `_task_get` | `_turn_get` |
| `DaemonTaskStore` | `DaemonTurnStore` |
| `state.task_store` and every `task_store` local | `turn_store` |
| `SessionLifecycle._watch` / `self._watchers` | `_supervise` / `self._supervisors` |
| `_close_watchers` | `_close_subscribers` |
| `task_id` naming an A2A task | `turn_id` |
| `task_identifier` on a background job | `job_id` |
| REST route parameter `context_id` | `session_id` |

### Frontend files

| Now | Becomes |
|---|---|
| `components/background-tasks-panel.tsx` | `components/background-jobs-panel.tsx` |

### Frontend symbols

| Now | Becomes |
|---|---|
| `abortSession()` | `cancelTurn()` |
| `A2ATask` | `A2ATurn` |
| `BackgroundTasksPanel` | `BackgroundJobsPanel` |
| `ShellTask` / `shellTasksFromMessages` / `shellTasksFromBackgroundJobs` | `ShellJob` / `shellJobsFromMessages` / `shellJobsFromBackgroundJobs` |
| `task_identifier` in `tool-event.ts`, `tool-views/`, the jobs panel | `job_id` |

### i18n

The message catalogue is part of the interface and gets the same treatment, in both `en.json` and `ja.json`.

| Now | Becomes |
|---|---|
| section `BackgroundTasksPanel` | `BackgroundJobsPanel` |
| `ToolViews.taskId` | `ToolViews.turnId` |
| `BackgroundJobsPanel.noProcessesDescription` | value changes from "Active jobs will appear here" to name processes consistently with the rest of its own section |

The `ToolDisplay.setTasks` / `updateTasks` keys and their "Setting task list" / "Updating task list" values stay: that is the to-do list, and it keeps the word.

### Prose

`peer` stops meaning a remote agent in `cli.md` and `architecture.md`. "The API" is replaced by the surface's name in `README.md`, `cli.md`, `architecture.md` and `tools.md`. The ending verbs settle into **end** (a session, user-facing), **reap** (a subtree, internal), **terminate** (a process signal, internal), **cancel** (a turn, over the wire) and **abort** (a turn, in-process), and the prose stops using `kill` and `stop` outside the CLI verb. The leftover "what spawned what" in `architecture.md` goes.

## What the persisted schema does

There is no migration and no compatibility shim. The tables and columns are renamed with everything else — `task_head`/`task_history`/`task_artifacts` become `turn_head`/`turn_history`/`turn_artifacts`, their `task_id` column becomes `turn_id`, `task_metadata` becomes `turn_metadata`, and the artifact indexes lose their `_context` suffix. An existing `history.db` will not load against the new schema, which is the accepted cost: transcripts are replayable and the database rebuilds itself, and carrying a dead vocabulary in storage forever to avoid deleting one file is the worse trade.

## Where the old words survive, and why

Three vocabularies are not ours, and renaming inside them would be renaming somebody else's thing.

The **A2A protocol** owns `contextId` and `taskId` on the wire, and the attribute names `Task.context_id`, `Message.context_id` and `Message.task_id` in the SDK's models. Every read of those attributes and every keyword passed to an a2a constructor keeps the protocol's spelling; what surrounds it is ours and is renamed. The same goes for `DefaultRequestHandler(task_store=…)`, whose keyword is a2a's even though the object handed to it is our turn store. This is the one place where a blanket rename actively breaks things — a renamed keyword is silently accepted by a pydantic model with a default, so the field is simply never set, and the failure surfaces much later as a turn persisted under an id nothing will look up. Every a2a constructor call is checked by AST after the sweep, not by eye.

The **a2a `TaskStore` base class** defines `save`/`get`/`delete`. Their parameter names are ours to choose — a2a calls them positionally everywhere — so they take `turn_id`, but the method names stay.

The **to-do list** keeps the word task, in `set_tasks`, `update_tasks`, `TaskItem`, `ChatTask` and the "Setting task list" strings. That is what a user calls it, and what the industry calls it.

## What is deliberately not renamed

`set_tasks`, `update_tasks`, `TaskItem` and the "task list" wording: that is the to-do list, the user-facing word for it is task, and changing it would trade a real collision for an invented one.

`abortToolCall`: it aborts a tool call. The name is already right.

`xeac ps` and `xeac kill`: the CLI is allowed to be idiomatic where the idiom is strong, and both are. The mapping from CLI verb to control-plane method gets written down in `cli.md` so the remaining divergences read as chosen rather than accidental.

`contextId` on the A2A wire, and `Task` where it is the A2A type: those are the protocol's words, not ours.

## Verification

Beyond the usual sweep — ruff, the layering check, an import of every module, `tsc --noEmit`, `check:events` — this change needs two things proved specifically, because a half-applied rename is exactly the failure mode.

A grep for each old name across the whole tree must come back empty, excluding `documentation/plans/`, which is an append-only record and keeps the words that were true when each plan was written. And a live run has to exercise every renamed control-plane method end to end: create a session, send it a message and read the `turn_id` back, cancel the turn, list and detach a job, read a turn, end the session. A rename that typechecks but was missed on one call site fails there and nowhere else.
