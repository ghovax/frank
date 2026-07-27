---
created: 2026-07-26T23:52:40Z
updated: 2026-07-27T00:14:00Z
commit: 98560a9
---

# A Second Subscription, and What It Costs to Accept One

Daisy already lets a ChatGPT subscription pay for model calls instead of an API key. The argument for that was never about OpenAI: it was that a person who is already paying a monthly fee for a coding model should be able to spend it here, and that the harness should be indifferent to which side of a paywall a model sits on. A Cursor subscription is the same argument with a different vendor, and the same people tend to hold both.

So this adds `cursor` next to `chatgpt`: an OAuth sign-in in Settings, its models in the picker, greyed until the plan is known to serve them, and a chat model behind it. From the outside the two are indistinguishable, which is the point. From the inside they have almost nothing in common, and that is what this plan is about.

## The premise that turned out to be wrong

The obvious expectation, and the one this work started from, was that a second subscription would be a second branch: reuse the token store, reuse the loopback callback, point a `ChatCodexModel`-shaped client at a different host. That is very close to how the ChatGPT provider was added, and it is wrong here for a reason worth stating precisely, because it changes what the work is.

ChatGPT's route is an ordinary chat API reached unusually. Codex's endpoint speaks the OpenAI *Responses* API — JSON over server-sent events, stateless with `store: false`, the whole history resent each turn. The unusual part is only the credential: present yourself as the Codex CLI, and a subscription-scoped token comes back. Once you have it, you are making a normal request.

Cursor has no chat API at all. What it exposes to its own CLI is `agent.v1.AgentService`, a Connect-RPC service over protocol buffers whose unit of work is not a completion but *a turn of an agent* — one that streams text and reasoning, asks the client to run tools, reads and writes a blob store, and expects to be told when each of those finished. There is no endpoint that takes messages and returns a message. Getting one assistant reply out of it means driving an agent protocol to the point where it produces one and then stopping.

That is why this is three new files rather than three new branches, and why the interesting decisions are all about reduction rather than translation.

## What was read, and what was taken

OpenCode does not ship Cursor support; six community plugins do, and they disagree about the protocol in instructive ways. Three were read closely enough to matter.

`ephraimduncan/opencode-cursor` and its maintained fork `otto-assistant/opencode-cursor` drive `AgentService/Run`, the bidirectional method, over a full-duplex HTTP/2 stream. Because Bun's `node:http2` cannot do that, both spawn a Node child process purely to hold the socket and ferry length-prefixed bytes over its stdin and stdout. `Yukaii/yet-another-opencode-cursor-auth` takes a different route through the same service: `RunSSE`, which is server-streaming, plus unary `BidiAppend` calls to push client messages into the stream it opened. Same protocol, but no full duplex — which means no HTTP/2 requirement, which means `httpx` can do it and Daisy needs no subprocess and no new dependency. That decided the transport.

The field numbers were not taken from any of them. `otto`'s generated bindings embed the service's own `FileDescriptorProto`, so the descriptor was decoded and read directly, and every number in `cursor_wire.py` comes from there. This mattered more than it sounds: the hand-rolled implementations have drifted from the current schema in ways that would have been inherited silently. `TokenDeltaUpdate.tokens` is an `int32` token count, which one plugin parses as text. `McpToolDefinition.input_schema` is a `bytes` field holding a serialized `google.protobuf.Value`, which is easy to get subtly wrong from traffic alone. `RequestContextEnv.workspace_paths` is repeated, not singular. Reading the descriptor cost an hour and removed a class of bug that testing could not have found without a subscription.

The login flow was taken as-is, because all three implement it identically and it is not something to be clever about. It is PKCE, but not OAuth as the ChatGPT provider does it: `cursor.com/loginDeepControl` is opened with a challenge and a client-chosen `uuid`, and the client then *polls* `api2.cursor.sh/auth/poll` with that `uuid` and the verifier until the browser side completes and the poll answers with an access and refresh token pair. There is no redirect. Nothing lands back on this machine.

That difference deletes the worst part of the ChatGPT sign-in rather than reproducing it. That flow needs a loopback server on port 1455 because OpenAI registered that redirect for the Codex client, so a sign-in can fail before it starts, with a `409` and a message about a port being in use, whenever a Codex CLI is also mid-login. Cursor's flow has no port to collide over. A sign-in either completes, is superseded, or times out.

## Which shape gives way

Cursor's turn is stateful: a conversation is a server-side identity, tool results are pushed into a stream that stays open across them, and the model continues inside that same stream. Daisy's turn is stateless: the harness owns the transcript, resends all of it every time, and expects one assistant reply per call. These do not compose, and one of them has to give.

Daisy's shape wins. Not for convenience — the stateful route is genuinely better on the wire, since it would let Cursor cache a prompt across a tool-call round trip and would let history arrive as structured turns rather than as rendered text. It loses because the harness compacts history, rewrites it, and replays sessions across process restarts. A conversation whose real state lived on Cursor's side would drift from the transcript Daisy believes in, and the transcript is the thing users read, sessions resume from, and compaction operates on. Keeping a live bidirectional stream alive across the harness's turn boundary — which is what `otto` does, with a per-conversation bridge cache and eviction sweeps — would put a second, invisible copy of the conversation somewhere the harness cannot see, and would make the correctness of a turn depend on whether that copy had been evicted.

So every call opens a fresh run. The whole conversation is rendered into the turn's user message, the tools are re-declared, and when the model calls one the stream ends and the call is handed back. Daisy runs the tool, appends the result, and calls again, where the next run sees it as history. This is the same decision `store: false` records for the Codex client, made for the same reason, and it is the one thing about this integration that is a choice rather than a constraint.

A first turn is sent as bare text, with no scaffolding around a single question. Once there is history it is labelled — `## User`, `## Assistant`, `## Assistant tool call: bash`, `## Tool result: bash` — because a model has to be able to tell its own previous output and a tool's result apart from what the user said, and a flattened blob of alternating prose does not let it.

## The system prompt does not travel inline

`AgentRunRequest` has a `custom_system_prompt` field, and using it would have been one line. It is not used, because the path that is known to work is a different one and this is not a protocol to guess at.

Cursor's `ConversationStateStructure` names its root prompt by *blob id*, and then asks for the blob over the KV channel while the turn is running. So the system prompt is hashed, its id goes into the conversation state, and the blob is handed over when requested — which means a turn involves answering a question the server asks mid-stream before the model has said anything. An unanswered KV request stalls the whole run, so a read this side cannot satisfy is answered empty rather than left hanging.

Turns are deliberately left out of the conversation state even though a field exists for them, and this is the one place a plausible-looking implementation fails outright: a turn's `user_message` is a blob reference too, and referring to blobs a fresh conversation has never stored produces "blob not found" rather than history. History goes in the message, not the state.

## The agent will reach for tools this client will not run

Cursor's agent has built-in tools — shell, read, write, grep, ls, diagnostics — and it reaches for them regardless of what the client offered, because its toolset is decided server-side. The community plugins execute them locally: spawn the shell, read the file, write the file. That is coherent for a plugin whose whole job is to be a model gateway.

It is not available here, and refusing is not a limitation but the point. Daisy has a permission mode, a confinement boundary, and a session that work is attributed to. A model client running a shell command has none of those in scope: it is below the harness, in a process whose job is to turn bytes into messages. Executing a command from there would put file writes and subprocesses outside every mechanism that exists to govern them, and would do it invisibly, because nothing in the transcript would record a tool the harness never dispatched.

So each one is declined, by name, using the protocol's own rejection variants — `ShellRejected`, `ReadRejected`, `WriteRejected` and the rest, which exist precisely for a client that says no. Declining explicitly matters as much as declining: the agent waits on an exec it asked for, so silence would stall the turn rather than move the model on to a tool it does have. The rejection echoes back the command or path it refused, so the agent's own transcript names what it was denied, and carries a reason telling it to use the tools under the `daisy` server instead. One exec is answered rather than refused: `request_context_args` asks what the machine looks like, which is context and not capability, so it gets an answer.

## Hand-rolled bytes, deliberately

`cursor_wire.py` is a protobuf codec and about thirty messages, written out rather than generated. `agent.proto` is five hundred messages describing an entire coding agent; generating it would put fifteen thousand lines of machine output in the tree to use six percent of it, and would add a protobuf runtime to a dependency list with no other use for one. The wire format is a tag, a wire type, and a length. Writing it out puts the field numbers where the code uses them, which is exactly where they need to be when a vendor moves them.

One call escapes this entirely. `GetUsableModels` is reachable over Connect's JSON encoding, so discovering which models an account serves is a plain JSON `POST` with no protobuf at all.

There is one piece of the protocol that is not engineering and should be named as such. `x-cursor-checksum` is a client-side obfuscation of a half-hour-rounded timestamp plus two truncated digests of the token. It proves nothing, protects nothing, and is reproduced here only because Cursor's own client sends it and a request without it is refused. The port was verified byte-for-byte against the reference implementation, because a checksum that is subtly wrong fails in a way that looks like an auth problem.

## What the interface does not show

The ChatGPT provider has usage meters: a rolling window and a weekly one, snapshotted from `x-codex-*` headers that ride on every response. The Cursor control has none, and this is a real difference rather than an unfinished edge.

Nothing Cursor returns on any call this makes reports the account's remaining allowance — not the model list, not the run stream, not the token deltas. There is no cheaper source to poll and no more expensive one either. The allowance surfaces exactly once, as `grpc-status 8` on a turn that has already been refused, which is reported as an error saying so. Showing an empty meter, or a meter derived from counting turns here, would be worse than showing nothing: the first is furniture and the second is a number the vendor never agreed to.

Two smaller asymmetries follow from the protocol. Cursor reports one running token total for a conversation rather than an input/output split, so that total is recorded as input — it is the number the context gauge needs, and inventing a split would be worse than reporting none. And nothing on this provider claims image input, because the agent service takes a text turn.

## Effort is part of the model's name

Every other provider in Daisy takes a reasoning effort as a setting. Cursor puts it in the model id: `claude-4.6-opus-high`, `gpt-5.4-medium`. So this provider ignores `reasoning_effort` entirely — picking the model already said it, and a second setting could only disagree with the first.

That has a visible consequence. Cursor gives every effort variant of a model the same display name, so a picker that trusted it would list "Claude 4.6 Opus" three times with no way to tell them apart. The effort is in the id, so it goes in the label.

## The offline list is a placeholder and nothing more

The `chatgpt` provider derives its model list from the models.dev catalog, filtered to the Codex-eligible set, so new models appear on their own. That cannot work here: a Cursor model id carries its effort, and names Cursor's own Composer family, none of which exists in a catalog of direct-API models.

So there is a short hand-written list, and it exists for exactly one purpose — so a signed-out user can see, greyed, what signing in would unlock. Nothing in it is ever reported as available. The real list is discovered live from `GetUsableModels`, and models an account serves that this version has never heard of are appended with their live names, so the list going stale costs a placeholder and never a capability.

## The thing to be careful about

This rides on the login flow Cursor's own CLI uses, against a service with no published contract for other clients. There is no version of this that is stable, and the ChatGPT provider's own history is the precedent: Anthropic banned the equivalent for Claude in February 2026. It is offered as an experiment, labelled as one in the interface, and the tokens it stores are password-equivalent and written mode 0600 outside the synced configuration file for the same reason the ChatGPT ones are.

Having two of these is also what turned one file into a folder. A single `chatgpt_auth.json` beside the databases was fine when it was the only one; two of them, with more likely, is a naming convention pretending to be a directory. So both now live in `oauths/`, one file per provider, in a directory created 0700 so a token added there later is protected by where it lives rather than by whoever remembers to chmod it. A file left over from the flat layout is moved in on first use, and signing out deletes it as well as the new one — the relocation fires on every read, not only after an upgrade, so a sign-out that forgot the old file would let the next read sign the account back in.

And it is unverified against the live service. Every piece that can be checked without a Cursor subscription has been: the wire codec round-trips, the framing survives arbitrary chunk boundaries, the built turn parses back to the structure the descriptor describes, and the checksum matches the reference byte for byte. What has not been checked is whether Cursor accepts it — which needs a paid account, and is the first thing to do with one.
