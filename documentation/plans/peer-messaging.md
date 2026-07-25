---
created: 2026-07-25T08:53:51Z
updated: 2026-07-25T09:16:00Z
commit: 2f437dc
---

# Peers Answer by Messaging, Not by Being Scraped

A session hands work to a peer and gets an answer back. The answer currently arrives by a route nobody designed: the calling session opens a subscription to the peer's event stream, waits for the edge where its turn stops running, reads the peer's last task out of the store, and then reconstructs prose from it by walking the task's result artifact, falling back to its final status message, pulling `kind: "text"` parts out of each and joining them with newlines. That is `_deliverable` and `_text_of` in `worker/peers.py`, and it is the wrong shape twice over. It reaches into another session's durable record to recover something the peer already knew and could simply have said; and it decides, on the peer's behalf and by string surgery, which fragments of its transcript constitute "the answer".

The primitive that removes all of it already exists in outline. `send_to_session` sends a message from one session to another, and an inbound message to a live session is injected into its current turn at the next safe point rather than queued behind it. Generalise that tool so a peer may aim it at the session that created it, and the answer becomes an ordinary message arriving in an ordinary context. Nothing scrapes anything. The peer decides what its answer is, in its own words, at the moment it is done — which is the only place that judgement can honestly be made.

This is also what A2A composition is supposed to look like. Two sessions, each addressable, exchanging messages. The waiting apparatus was a workaround for the absence of a return path, and once the return path exists it is not a smaller version of the same thing — it is nothing at all.

## What replaces the waiting machinery

`create_session` creates the peer, hands it the brief, and returns its id. That is the whole call. There is no settle window, no background job, no `task_identifier`, and no completion message synthesised by the harness, because the peer will send its own. The calling session carries on with whatever does not depend on the answer and ends its turn when everything left does; the peer's message wakes it exactly the way a person's message would.

`send_to_session` becomes `send_message`, with a recipient set of "any session you created, plus the session that created you". Up and down the tree, not sideways: a sibling is reached by asking the parent to relay, which keeps the addressable set equal to the set a session can already name. The rename is not cosmetic. `send_to_session` reads as a verb for driving a subordinate, and the tool is now equally the way a subordinate reports back; a peer told to use `send_to_session` on its parent is being told to do something the name argues against.

The one thing the harness must still say on its own is that a peer died without reporting. A child that crashes, is killed, or exits mid-turn leaves its caller waiting for a message that will never arrive, and the old polling wait covered that case for free by watching the process rather than the conversation. The daemon knows when it reaps a session and who its parent is, so it sends the parent a notice carrying the child's id, terminal state, and exit reason. This is the only harness-authored message in the design, and it exists because it reports a fact the peer is by definition unable to report.

## A peer message must not read as the user

Inbound messages become turns, and a turn carries a `TurnKind` — today `user`, `autonomous`, or `compaction`. A peer's reply sent through the existing path would arrive with `role: "user"` and no kind of its own, which means the model reads it as *the user speaking*, and the desktop client renders it in the user's own message style. A session would be told its peer's report is an instruction from the person it is working for, and a person watching the transcript would see words attributed to them that they did not write.

So `TurnKind.PEER` is not a nicety. `send_message` stamps it, the worker threads it through the turn envelope the way the autonomous and compaction kinds already are, and both the model and the UI learn where the message came from. On the model side this is what lets the prompt say "a peer reported this" rather than leaving the model to infer authorship. On the UI side it is a new message role, rendered as a peer's report rather than as the user's turn.

## What the peer needs to know

A session is currently told, in `agent_context.md`, that "another session may have created you, and may be waiting on your result" — and is given no way to find out which one. The parent id is in the worker's assignment and never reaches the model. So the turn context gains `parent_session`, and the agent context says plainly: when you are done, send your answer there. It cannot be enforced, and pretending otherwise would be worse than saying so; what can be done is to make the address available, name the obligation once, and make the failure visible through the termination notice rather than as an infinite wait.

## The frontend

The desktop client is affected by this change and by the three that preceded it, and some of that is already stale on `main`. The research below is what is actually in `web/src`, not what ought to be.

The client's `attachSession` calls `/sessions/{id}/attach`, which is the daemon's control-plane route — the one that gained a `turn` frame when `xeac wait` was fixed. Its `SessionStreamFrame` union is `snapshot | live | done` and does not know that frame exists. Nothing crashes, because the reducer is an if/else chain that ignores what it does not recognise, but the type is wrong and the client is throwing away the only direct signal that a turn ended. It currently infers the same thing by watching `sessionRunning` flip, which is derived from polled session-list rows. Teaching it the frame is both a type fix and a latency fix.

`rest/routes/sessions.py` still serves `/sessions/{context_id}/stream`, a second implementation of the same snapshot/live/done stream. Nothing in the frontend calls it — the only `/stream` reference in `web/src` is the git-status stream — and it has already diverged from the route that is actually used, since it never gained the turn frame. It is dead and should go rather than sit there as a second answer to a question that has one.

`createSession` in `web/src/lib/api.ts` types the response as `{ id, token }`. The daemon now also returns `parent` and `permission_mode`, precisely so a creator can see that its request was clamped. The GUI creates sessions as a human, so nothing is silently clamped today, but the fields are part of the contract and the type should carry them.

`tool-views/index.tsx` switches on tool name and has no case for any session tool, so `create_session`, `send_message`, `read_session`, `list_sessions` and `end_session` all render through `GenericView` — a raw key/value dump of the arguments. For `create_session` that means the brief, which is often several paragraphs, is rendered as an unformatted blob in the transcript. These are among the most consequential calls a session makes and they currently look the least legible. The result renderer has the same gap: it dispatches on the result `code`, and none of the session codes are known to it.

The generated wire-event types under `web/src/lib/generated` come from `protocol/events.py` via `scripts/generate_event_schema.py`, and `bun run check:events` diffs them in CI. Anything this plan changes in that module has to be regenerated in the same commit or the check fails.

Terminology in the client has the same drift the backend had before the last commit. `sessions-sidebar.tsx` describes "the sessions an agent spawns", the i18n keys are `showSpawnedSessions` and `hideSpawnedSessions` in both `en.json` and `ja.json`, `use-chat.ts` twice explains that "a spawned agent is its own session", `api.ts` documents `parent` as "the session that spawned this one", and `remote-agents-panel.tsx` still calls remote agents the ones "this harness can delegate to". None of it is wrong in the sense of broken, and all of it is the vocabulary the backend has now stopped using.

## The changes

| # | Change | Where | Why |
|---|---|---|---|
| 1 | `send_to_session` becomes `send_message(session, message)`; recipients are the caller's children plus its parent | `runtime/tools/sessions.py`, new `prompts/tool_send_message.md` | The one session-to-session primitive, named for what it does rather than for one direction of travel |
| 2 | `create_session` returns the peer's id and stops — no settle, no handle, no synthesised result | `runtime/tools/sessions.py`, `prompts/tool_create_session.md` | The peer sends its own answer; there is nothing left for the caller to wait on |
| 3 | Delete `await_result`, `_await_idle`, `_deliverable`, `_text_of`, `_finished`, `_await_and_report`, `_start_and_settle`, `_maybe_object`, `_peer_handle` | `worker/peers.py`, `runtime/tools/sessions.py` | The scraping and every scaffold that existed only to feed it |
| 4 | Delete the `peer_session` background kind, its identifier prefix, `PEER_SYNC_WINDOW_SECONDS`, and `prompts/peer_session_started_note.md` | `runtime/background.py`, `base/tuning.py`, `prompts/` | A peer's turn is no longer a background job of the caller's |
| 5 | Allow `session.send` upward to the caller's parent; everything else stays subtree-only | `daemon/api.py::_refuse_session_caller` | Without it the reply is refused 403; the narrowest widening that makes a return path exist |
| 6 | Add `TurnKind.PEER`; `send_message` stamps it; the worker threads it through the turn envelope | `protocol/turn_record.py`, `runtime/tools/sessions.py`, `worker/peers.py`, `worker/session.py`, `worker/turn.py` | Otherwise a peer's report arrives as `role: "user"` and is read, by model and by person, as the user speaking |
| 7 | The daemon notifies a parent when it reaps a child, with the child's id, terminal state and exit reason | `daemon/lifecycle.py` | A peer that dies cannot report that it died; this is the one message the harness must author |
| 8 | Add `parent_session` to the turn context JSON, and tell the session in `agent_context.md` to report there | `runtime/turnloop.py`, `prompts/agent_context.md` | A session is told a peer may be waiting on it and is not told who |
| 9 | Rewrite the peer section of the system prompt; drop the `peer-…` handle from the background-handle guidance and `read_task` | `prompts/system_prompt.md`, `prompts/read_task_background_handle.md`, `tools/registry.py` | Both currently describe a completion message and a handle class that will not exist |
| 10 | `SessionStreamFrame` gains `{ kind: "turn"; seq: number; running: boolean }`, and the reducer uses it as the end-of-turn signal | `web/src/lib/api.ts`, `web/src/lib/use-chat.ts` | The client already receives this frame and discards it, inferring the same fact from polled rows instead |
| 11 | Add a `peer` role to `ChatMessage`, reduce a `PEER`-kind turn into it, and render it as a peer's report | `web/src/lib/use-chat.ts`, `web/src/components/chat-panel.tsx` | A peer's message is currently indistinguishable from something the user typed |
| 12 | Tool-call and tool-result views for `create_session`, `send_message`, `read_session`, `list_sessions`, `end_session` | `web/src/components/tool-views/index.tsx` | The most consequential calls a session makes render as a raw argument dump, brief included |
| 13 | `createSession`'s response type gains `parent` and `permission_mode` | `web/src/lib/api.ts` | The daemon returns them so a creator can see a clamp; the type should carry the contract |
| 14 | Delete `/sessions/{context_id}/stream` | `rest/routes/sessions.py` | A second implementation of the attach stream with no consumer, already diverged from the one in use |
| 15 | Unify terminology in the client: sidebar comments, the `showSpawnedSessions`/`hideSpawnedSessions` keys in `en.json` and `ja.json`, the `use-chat.ts` and `api.ts` comments, and the remote-agents panel | `web/src/components/`, `web/src/lib/`, `web/messages/` | Sessions are created, not spawned, and nothing delegates any more |
| 16 | Regenerate `events.schema.json` and `events.ts` | `scripts/generate_event_schema.py`, `web/src/lib/generated/` | `bun run check:events` diffs them; a protocol change without a regenerate fails CI |
| 17 | Update the documentation | `documentation/tools.md`, `cli.md`, `architecture.md`, `agents-and-skills.md`, `README.md`, `.agents/skills/harness-configuration/SKILL.md` | Every one of them describes the tool returning what the peer produced |

## What is deliberately not changing

`ask_user` stays as it is. It blocks: it parks the turn as a human-in-the-loop gate with structured options and resumes on the answer, which is a different mechanism from a message sent and forgotten. Folding both into one tool whose semantics flip on the recipient would trade a clear pair for a confusing single.

`xeac send --wait` and the CLI's `_follow` are untouched. A person waiting at a terminal genuinely does want to block until the work is done and see the result printed, and the turn-boundary frame that makes that race-free is the same frame the desktop client is about to start using. The CLI is for people; nothing about the composition path between sessions changes what it should do.

Sessions still cannot message siblings directly, and the subtree scoping on every other control-plane verb is unchanged. The only new authority a session gains from this plan is the ability to send one message upward, to the session that created it.
