---
created: 2026-07-26T23:52:40Z
updated: 2026-07-27T02:48:00Z
commit: 98560a9
---

# A Second Subscription, and What It Costs to Accept One

Daisy already lets a ChatGPT subscription pay for model calls instead of an API key. The argument for that was never about OpenAI: it was that a person who is already paying a monthly fee for a coding model should be able to spend it here, and that the harness should be indifferent to which side of a paywall a model sits on. A Cursor subscription is the same argument with a different vendor, and the same people tend to hold both.

So this adds `cursor` next to `chatgpt`: an OAuth sign-in in Settings, whatever models the account turns out to serve, and a chat model behind them. From the outside the two are indistinguishable, which is the point. From the inside they have almost nothing in common, and that is what this plan is about.

## The premise that turned out to be wrong

The obvious expectation, and the one this work started from, was that a second subscription would be a second branch: reuse the token store, reuse the loopback callback, point a `ChatCodexModel`-shaped client at a different host. That is very close to how the ChatGPT provider was added, and it is wrong here for a reason worth stating precisely, because it changes what the work is.

ChatGPT's route is an ordinary chat API reached unusually. Codex's endpoint speaks the OpenAI *Responses* API — JSON over server-sent events, stateless with `store: false`, the whole history resent each turn. The unusual part is only the credential: present yourself as the Codex CLI, and a subscription-scoped token comes back. Once you have it, you are making a normal request.

Cursor has no chat API at all. What it exposes to its own CLI is `agent.v1.AgentService`, a Connect-RPC service over protocol buffers whose unit of work is not a completion but *a turn of an agent* — one that streams text and reasoning, asks the client to run tools, reads and writes a blob store, and expects to be told when each of those finished. There is no endpoint that takes messages and returns a message. Getting one assistant reply out of it means driving an agent protocol to the point where it produces one and then stopping.

That is why this is three new files rather than three new branches, and why the interesting decisions are all about reduction rather than translation.

## What was read, and what was taken

OpenCode does not ship Cursor support; six community plugins do, and they disagree about the protocol in instructive ways. Three were read closely enough to matter.

`ephraimduncan/opencode-cursor` and its maintained fork `otto-assistant/opencode-cursor` drive `AgentService/Run`, the bidirectional method, over a full-duplex HTTP/2 stream. Because Bun's `node:http2` cannot do that, both spawn a Node child process purely to hold the socket and ferry length-prefixed bytes over its stdin and stdout. `Yukaii/yet-another-opencode-cursor-auth` takes a different route through the same service: `RunSSE`, which is server-streaming, plus unary `BidiAppend` calls to push client messages into the stream it opened. Same protocol, but no full duplex — which means no HTTP/2 requirement, which means `httpx` can do it and Daisy needs neither a subprocess nor a protobuf runtime. That decided the transport.

The field numbers were not taken from any of them. `otto`'s generated bindings embed the service's own `FileDescriptorProto`, so the descriptor was decoded and read directly, and every number in `cursor_wire.py` comes from there. This mattered more than it sounds: the hand-rolled implementations have drifted from the current schema in ways that would have been inherited silently. `TokenDeltaUpdate.tokens` is an `int32` count of generated tokens, which one plugin parses as text. `McpToolDefinition.input_schema` is a `bytes` field holding a serialized `google.protobuf.Value`, which is easy to get subtly wrong from traffic alone. `RequestContextEnv.workspace_paths` is repeated, not singular. Reading the descriptor cost an hour and removed a class of bug that testing could not have found without a subscription.

The login flow was taken as-is, because all three implement it identically and it is not something to be clever about. It is PKCE, but not OAuth as the ChatGPT provider does it: `cursor.com/loginDeepControl` is opened with a challenge and a client-chosen `uuid`, and the client then *polls* `api2.cursor.sh/auth/poll` with that `uuid` and the verifier until the browser side completes and the poll answers with an access and refresh token pair. There is no redirect. Nothing lands back on this machine.

That difference deletes the worst part of the ChatGPT sign-in rather than reproducing it. That flow needs a loopback server on port 1455 because OpenAI registered that redirect for the Codex client, so a sign-in can fail before it starts, with a `409` and a message about a port being in use, whenever a Codex CLI is also mid-login. Cursor's flow has no port to collide over. A sign-in either completes, is superseded, or times out.

## Waiting is a library's job; how long to wait is the harness's

The first version of the sign-in poll carried five constants of its own — a base delay, a ceiling, a multiplier, an attempt budget, and a tolerance for consecutive errors — and a loop to apply them. Every one of those was a thing already solved twice over.

The mechanics are tenacity's. A pause that widens to a ceiling, a deadline that ends the attempt, and a predicate deciding which failures are worth asking again about is exactly what that library is, and writing it by hand is how the tally of "three consecutive errors" got in: an invented rule, copied from a reference implementation, that treated a permanent refusal from Cursor as two-thirds of a transient network blip. Retrying by *kind* instead is both simpler and better — a refusal now fails on the first one rather than the third, while a person who loses their connection mid-sign-in keeps their window.

The values are the harness's, and they were always meant to be. `base/tuning.py` already holds every duration in Daisy as a named, described, policy-scaled `Tunable`; five module constants sitting next to the code that read them was not a new problem needing a new answer, it was ignoring the existing one. So the interval, the ceiling, and the give-up window joined the rest of them, and the library got the loop.

The same reasoning reached two other waits: a stood-down daemon waiting for the winner's socket to answer, and a restart waiting for the predecessor to exit. Both were hand-rolled loops around bare literals — `30.0`, `0.5`, `0.05`, `0.1` — and both are now the library's, over named tunables. Two nearby loops were deliberately left alone: the accessibility readiness probe, whose values are *already* tunables and whose four lines tenacity would only complicate, and `settle()`, which is not a retry at all but a convergence detector that waits for a value to repeat.

## Which shape gives way, and how far

Cursor's turn is stateful: a conversation is a server-side identity, and the model expects to continue inside a stream that stays open across tool calls. Daisy's turn is stateless: the harness owns the transcript and expects one assistant reply per call. These do not compose, and something has to give.

What gives is the *authority*, not the mechanism. Daisy's transcript stays the only source of truth. Cursor's own idea of the conversation is kept and reused, but strictly as a **cache** of that transcript — which turns out to be exactly what the protocol supports, and is the thing the first version of this work got wrong by not doing at all.

Every turn is a fresh HTTP run. Each turn Cursor completes ends with a `conversationCheckpointUpdate` carrying its whole `ConversationStateStructure`, and `AgentRunRequest.conversation_state` takes one back — so a conversation resumes by *sending its state up with the request*, not by holding a socket open. When a checkpoint is held for this conversation, it goes back and only the messages Cursor has not seen are rendered into the turn. When one is not, the entire conversation is rendered instead.

The fallback is what makes the cache safe rather than merely fast. A resume happens only when the transcript still *begins with* exactly the messages the checkpoint was made from, tested by fingerprinting them. A compaction, an edit, a fresh worker process, or an expired entry all fail that test, and failing it means resending everything — which is precisely the behaviour that existed before the cache. Cursor can therefore never be resumed into a conversation that differs from the one Daisy believes in, because a difference is what a miss *is*. The earlier objection to resuming — that it would put an invisible second copy of the conversation somewhere the harness cannot see — was answered by making the copy disposable and its staleness detectable, rather than by refusing to keep one.

Two details of the resume are not obvious. The checkpoint has its `pending_tool_calls` stripped before it goes back: a checkpoint is captured mid-turn and this client ends its run the moment the model calls a tool, so the state it captured can name a call nobody is going to answer, and resuming with it listed invites the server to wait forever. The maintained plugin strips the same field for the same reason after a user interrupt. And the checkpoint travels with the blob store captured alongside it, because a checkpoint refers to blobs by id and a resume that cannot serve them fails with "blob not found".

A first turn — or the new tail of a resumed one — is sent as bare text, with no scaffolding around a single question. Once there is history to render it is labelled: `## User`, `## Assistant`, `## Assistant tool call: bash`, `## Tool result: bash`. A model has to be able to tell its own previous output and a tool's result apart from what the user said, and a flattened blob of alternating prose does not let it.

One line of that scaffolding is load-bearing. When the transcript carries tool results it says so explicitly: those calls have already run, do not run them again. The protocol has no way to hand a structured tool result back outside the stream that asked for it, so by the time the model reads a completed call it is prose — and Cursor's agent is built to keep working, so a transcript ending in "I ran `ls`, here is the output" reaches it as a new turn it could reasonably satisfy by running `ls`. The maintained plugin carries the same instruction in both of its recovery prompts, which is what makes this a known failure mode rather than a hypothetical one.

## What caching is, and what a client can do about it

Cursor bills the token accounting a subscription's credit pool is denominated in, and cache reads run at roughly a fifth of input rate. Input tokens are the dominant term in an agentic loop, so whether the server can treat a conversation's prefix as already-read is worth real money against a fixed monthly pool.

**No client does anything about caching, and there is nothing for a client to do.** The service descriptor has no cache field of any kind — no `cache_control`, no ephemeral marker, nothing to mark a prefix as reusable — and none of the plugins sends anything of the sort. The one place "cache" appears in any of them is a per-model price table used to report cost, and a client-side memo of the system prompt's blob id. Caching is entirely the server's business.

The only lever a client has is whether the conversation is resent or *referred to*, which is the checkpoint. That is now used, so this provider has the same lever as the clients that have it, pulled the same way. What remains unmeasurable from here is how much it is worth: the prompt sent on a cache miss is append-only, so ordinary prefix caching should still hit much of it even without a checkpoint, while a cache scoped per conversation id would miss entirely. With the checkpoint in place the question matters much less, because the resumed path does not resend the prefix at all.

## Every capability, and where each came from

The three clients disagree, and each got something right. This is what was taken, and what was done differently on purpose.

| Capability | ephraimduncan | otto | Yukaii | Here |
|---|---|---|---|---|
| Transport | `Run` / HTTP2 + Node subprocess | same, pooled | `RunSSE` + `BidiAppend` | **Yukaii's** — no subprocess, no HTTP/2, no protobuf runtime |
| Model list | `GetUsableModels` | `AvailableModels`, falling back | `GetUsableModels` | **both**: the list from the endpoint whose ids a run accepts, the rest from the one that knows it |
| Context windows | hardcoded default | from `AvailableModels` | hardcoded default | **`AvailableModels`, then the live checkpoint**, which outranks it |
| Variant selection | none | `RequestedModel` + parameters | none | **otto's**, when discovery knows the variant |
| Token accounting | partial | output summed, input from checkpoint | estimated locally | **otto's**, which is the only correct reading |
| System prompt | root-prompt blob | root-prompt blob | inline | **root-prompt blob** |
| Structured turns in state | yes — **fails** | removed | n/a | **removed**, following otto's correction |
| Checkpoint resume | yes, 30-min TTL | yes, 30-min TTL | no | **yes**, with a prefix-fingerprint guard neither has |
| Built-in tools | executed in-process | executed in-process | executed in-process | **9 translated to harness tools**, 6 refused by name, 0 unanswered |
| Backend fallback | none | none | api5 hosts, behind a flag | **api5 hosts**, automatic, only before output |
| Silent-model watchdog | yes | yes, plus stall recovery | yes | **yes** |
| Client version | `cli-2026.01.09-231024f` | same | older | **the newest of them** |

Two entries deserve more than a row.

**Built-in tools.** Cursor's agent can announce thirty-two different tools, but only fifteen of them are things it asks the *client* to run — the rest it does server-side, which is why a web search or a semantic search never reaches this code at all. Those fifteen, plus the harness's own tools coming back and one question about the machine, are the seventeen exec kinds a run has to be able to answer. Answering all of them is not tidiness: an exec left unanswered is an agent waiting for a result that never comes, so a missing kind costs a stalled turn, and building the table below is what revealed that seven of them had no answer at all.

Cursor's agent reaches for these regardless of what the client offered, because its toolset is decided server-side. All three plugins run them inside the plugin, which is coherent for a model gateway and not available here: a model client executing a shell command has no permission mode, no confinement boundary, and no session to attribute the work to, and nothing in the transcript would record a tool the harness never dispatched. The earlier version of this declined all of them, which was safe and unhelpful — a model that habitually reaches for `read` got refused over and over. Now a built-in with a counterpart among the harness's own tools is *translated* into a call on that tool and handed back like any other. The agent gets what it asked for, and it happens where the harness can govern it.

| Cursor asks for | Becomes | Note |
|---|---|---|
| `shell` | `bash` | the command is the model's own text |
| `shell_stream` | `bash` | same tool; only the result variant differs |
| `background_shell_spawn` | `bash` background | the harness already has exactly this |
| `read` | `read_file` | |
| `write` | `write_file` | an empty body is a real write, so only a missing path disqualifies it |
| `ls` | `bash` read-only | a fixed `ls -la`; only the directory comes from the agent |
| `fetch` | `fetch_url` | |
| `list_mcp_resources` | `list_mcp_resources` | field for field, same spelling |
| `read_mcp_resource` | `read_mcp_resource` | field for field |
| `mcp` | the call itself | the harness's own tools coming back; no translation needed |
| `request_context` | answered directly | a question about the machine, not a tool |
| `delete` | **refused** | synthesizing an `rm` would mean this code decided to remove a file |
| `grep` | **refused** | output modes, context lines, type filters and multiline do not survive being flattened into one command line, and the nearest harness tool searches by meaning rather than by pattern |
| `diagnostics` | **refused** | no counterpart |
| `record_screen` | **refused** | no counterpart |
| `computer_use` | **refused** | Cursor describes it as a list of low-level actions while the harness's screen control takes a plain-language instruction; bridging those would be invention rather than translation |
| `write_shell_stdin` | **refused** | addresses a background shell by an id only a client that spawned it would hold |

A translation is also refused when the running agent was not given the tool it would map to, because inventing a capability an agent was configured without is worse than saying no — which is also what makes the screen-control case self-resolving, since that tool is opt-in and simply absent when it is off.

Every refusal uses the variant the protocol provides for it, and the protocol is not consistent about the name: `rejected` for tools a client may decline, `error` for ones it may only fail, `failure` for one. All three have the same shape from here — some identifying strings, then a reason — so one builder covers them, and what varies is a field number and how many strings precede the reason. `read_mcp_resource` is the case that forced that to be explicit: it takes a server and a uri, but its refusal names only the uri.

**Backend fallback.** Cursor moves this service between hosts, and Yukaii keeps the two agent hosts behind an environment flag because its runtime could not reach them. Here they are simply tried, in order, and only when a run fails *before producing anything* — a run that has already emitted text is never retried, because replaying it would duplicate its output.

## Where this stands against the clients that have been run

The run request matches the simpler plugin field for field — `conversation_state`, `action`, `model_details` with the id in all three name fields, `conversation_id` — plus `requested_model` when discovery knows which variant a model is, which is otto's addition.

There is one place where following the simpler plugin would have been a mistake, and reading both is what caught it. It builds structured turns into `conversation_state.turns`, each carrying a serialized `UserMessage`. The maintained fork tore that out with a comment explaining why: the server reads `AgentConversationTurnStructure.user_message` as a blob *reference*, so turns referring to blobs a fresh conversation never stored fail with "blob not found". Its replacement is history embedded as text in the user message. Following the newer code here rather than the simpler code is the difference between working and not.

The client version is `cli-2026.01.09-231024f` because that is the newest string any working client actually sends, not because of what today's date is. The header set is the one paired with *this* transport rather than the union of every set seen: the plugins holding an HTTP/2 stream open for `Run` send no checksum, timezone or streaming hint, the one using `RunSSE` sends all three, and assembling a superset would invent a fourth combination nobody has run.

One capability is present in all three and absent here on purpose: the sticky live bridge that keeps a single HTTP/2 stream open across a tool round trip. It delivers a *structured* tool result, which the checkpoint path cannot. It also brings a bridge pool, TTL eviction, stall recovery, cancel handling and heartbeat keepalives — the bulk of otto's complexity — and it is the one design that genuinely does put conversation state somewhere Daisy cannot see. The checkpoint gets most of its benefit at none of that cost.

## The system prompt does not travel inline

`AgentRunRequest` has a `custom_system_prompt` field, and using it would have been one line. It is not used, because the path that is known to work is a different one and this is not a protocol to guess at.

Cursor's `ConversationStateStructure` names its root prompt by *blob id*, and then asks for the blob over the KV channel while the turn is running. So the system prompt is hashed, its id goes into the conversation state, and the blob is handed over when requested — which means a turn involves answering a question the server asks mid-stream before the model has said anything. An unanswered KV request stalls the whole run, so a read this side cannot satisfy is answered empty rather than left hanging.

The blob store is therefore part of a conversation's state and not merely a detail of one turn, which is why a checkpoint is only worth keeping alongside the blobs captured with it. A resume that cannot answer for a blob its checkpoint names fails with "blob not found", and that is also why turns are never written into the conversation state directly: a turn's `user_message` is a blob reference too, and referring to blobs a fresh conversation has never stored produces that same failure rather than history. History goes in the message; state goes in the checkpoint.

## Hand-rolled bytes, deliberately

`cursor_wire.py` is a protobuf codec and about thirty messages, written out rather than generated. `agent.proto` is five hundred messages describing an entire coding agent; generating it would put fifteen thousand lines of machine output in the tree to use six percent of it, and would add a protobuf runtime to a dependency list with no other use for one. The wire format is a tag, a wire type, and a length. Writing it out puts the field numbers where the code uses them, which is exactly where they need to be when a vendor moves them.

Model discovery escapes it entirely. Both `GetUsableModels` and `AvailableModels` are reachable over Connect's JSON encoding, so learning which models an account serves, how large their windows are and how their variants are routed is three plain JSON `POST`s with no protobuf at all.

There is one piece of the protocol that is not engineering and should be named as such. `x-cursor-checksum` is a client-side obfuscation of a half-hour-rounded timestamp plus two truncated digests of the token. It proves nothing, protects nothing, and is reproduced here only because Cursor's own client sends it and a request without it is refused. The port was verified byte-for-byte against the reference implementation, because a checksum that is subtly wrong fails in a way that looks like an auth problem.

## What the interface does not show

The ChatGPT provider has usage meters: a rolling window and a weekly one, snapshotted from `x-codex-*` headers that ride on every response. The Cursor control has none, and this is a real difference rather than an unfinished edge.

Nothing Cursor returns on any call this makes reports the account's remaining allowance — not the model list, not the run stream, not the token deltas. There is no cheaper source to poll and no more expensive one either. The allowance surfaces exactly once, as `grpc-status 8` on a turn that has already been refused, which is reported as an error saying so. Showing an empty meter, or a meter derived from counting turns here, would be worse than showing nothing: the first is furniture and the second is a number the vendor never agreed to.

One smaller asymmetry follows from the protocol: nothing on this provider claims image input, because the agent service takes a text turn.

## Effort is part of the model's name

Every other provider in Daisy takes a reasoning effort as a setting. Cursor puts it in the model id: `claude-4.6-opus-high`, `gpt-5.4-medium`. So this provider ignores `reasoning_effort` entirely — picking the model already said it, and a second setting could only disagree with the first. The effort does still reach the backend as a parameter, because `RequestedModel` carries the variant's parameter values, but it is *read off the id* rather than asked for separately: discovery is what knows that `claude-4.6-opus-high` means that model at that effort.

That has a visible consequence. Cursor gives every effort variant of a model the same display name, so a picker that trusted it would list "Claude 4.6 Opus" three times with no way to tell them apart. The effort is in the id, so it goes in the label.

## Nothing about the models is written down

The `chatgpt` provider derives its model list from the models.dev catalog, filtered to the Codex-eligible set, so new models appear on their own. That cannot work here, and checking rather than assuming settled it: models.dev carries a hundred and seventy-three providers and Cursor is not among them, nor could it be — a Cursor model id carries its reasoning effort, and names Cursor's own Composer family, neither of which exists in a catalog of direct-API models.

The first attempt at this filled the gap with a short hand-written list, justified as a way for a signed-out user to see, greyed, what signing in would unlock. That justification does not survive contact with the thing it describes: a hand-written list is a guess about somebody else's subscription, it is stale the day it lands, and dressing it in the catalog's clothes makes it look like knowledge. There is nothing truthful to show before a sign-in, so nothing is shown — the picker offers the provider, its sign-in control, and no models until there is an account to ask.

Asked, the account answers completely, because two endpoints between them know everything the catalog needed. `GetUsableModels` returns the ids a run request accepts verbatim — it answers with the very message type the request echoes back — so it owns the list. `AvailableModels` is the only place the service states a context window, tucked inside each variant's parameters, so it owns those; it is reachable over Connect's JSON encoding, so discovery needs no protobuf at all. A failure there costs windows rather than the whole catalog.

Joining them takes one deliberate decision. `AvailableModels` answers with base models and variants while a run id is effort-suffixed, so a window is looked up first by a variant's own `legacySlug`, which matches exactly, and then by the longest base name the id starts with. Where a base name's variants disagree — a model offered at both its normal window and an extended one — the smallest is kept, because a window that reads too large overruns the model while one that reads too small only compacts early. And nothing is requested by name: `AvailableModels` takes an `additionalModelNames` list that makes the service mention models it would otherwise omit, and reaching for it would mean hardcoding model names in order to discover models.

One number survives all of this, and it is the best of them. Every checkpoint the server sends during a turn carries `ConversationTokenDetails`, which states both how full the conversation is and the window it is filling. That is per-account, per-model, and measured rather than published, so it outranks the catalog the moment a turn has run. What is left is a single floor for a model the catalog named without a window, set to the smallest window any model on the service currently has — under-promising, in the direction that compacts early rather than overruns.

That same field also fixed an accounting bug that had already shipped in the first version of this work. `TokenDeltaUpdate` is a delta of *generated* tokens and has to be summed; the first version took the last one it saw and reported it as the prompt size. The prompt size is `usedTokens` from the checkpoint, reported whole, so the largest seen in a turn is the one to keep — an early checkpoint would otherwise pin the meter below the real figure. So this provider now reports a genuine input/output split rather than one number standing in for both.

## The thing to be careful about

This rides on the login flow Cursor's own CLI uses, against a service with no published contract for other clients. There is no version of this that is stable, and the ChatGPT provider's own history is the precedent: Anthropic banned the equivalent for Claude in February 2026. It is offered as an experiment, labelled as one in the interface, and the tokens it stores are password-equivalent and written mode 0600 outside the synced configuration file for the same reason the ChatGPT ones are.

Having two of these is also what turned one file into a folder. A single `chatgpt_auth.json` beside the databases was fine when it was the only one; two of them, with more likely, is a naming convention pretending to be a directory. So both now live in `oauths/`, one file per provider, in a directory created 0700 so a token added there later is protected by where it lives rather than by whoever remembers to chmod it.

Nothing reads the old location. The first version of that move relocated a leftover file on first use, so an upgrading user would not be signed out — and getting even that small kindness right took a second attempt, because the relocation fired on every read rather than only after an upgrade, which let a sign-out be undone by the next read. That is the argument against it in one sentence: a path function that knows the shape of every layout the harness ever had is a path function that grows a bug per layout, and a sign-out has to delete from all of them. The cost of not having it is one browser round trip, once. So a token where this used to put one is simply not found, the provider reports itself signed out, and there is exactly one place a token can be.

And it is unverified against the live service. Every piece that can be checked without a Cursor subscription has been: the wire codec round-trips, the framing survives arbitrary chunk boundaries, the built turn parses back to the structure the descriptor describes, and the checksum matches the reference byte for byte. What has not been checked is whether Cursor accepts it — which needs a paid account, and is the first thing to do with one.
