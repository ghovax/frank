---
created: 2026-07-26T23:52:40Z
updated: 2026-07-27T01:52:00Z
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

## Which shape gives way

Cursor's turn is stateful: a conversation is a server-side identity, tool results are pushed into a stream that stays open across them, and the model continues inside that same stream. Daisy's turn is stateless: the harness owns the transcript, resends all of it every time, and expects one assistant reply per call. These do not compose, and one of them has to give.

Daisy's shape wins. Not for convenience — the stateful route is genuinely better on the wire, since it would let Cursor cache a prompt across a tool-call round trip and would let history arrive as structured turns rather than as rendered text. It loses because the harness compacts history, rewrites it, and replays sessions across process restarts. A conversation whose real state lived on Cursor's side would drift from the transcript Daisy believes in, and the transcript is the thing users read, sessions resume from, and compaction operates on. Keeping a live bidirectional stream alive across the harness's turn boundary — which is what `otto` does, with a per-conversation bridge cache and eviction sweeps — would put a second, invisible copy of the conversation somewhere the harness cannot see, and would make the correctness of a turn depend on whether that copy had been evicted.

So every call opens a fresh run. The whole conversation is rendered into the turn's user message, the tools are re-declared, and when the model calls one the stream ends and the call is handed back. Daisy runs the tool, appends the result, and calls again, where the next run sees it as history. This is the same decision `store: false` records for the Codex client, made for the same reason, and it is the one thing about this integration that is a choice rather than a constraint.

A first turn is sent as bare text, with no scaffolding around a single question. Once there is history it is labelled — `## User`, `## Assistant`, `## Assistant tool call: bash`, `## Tool result: bash` — because a model has to be able to tell its own previous output and a tool's result apart from what the user said, and a flattened blob of alternating prose does not let it.

One line of that scaffolding is load-bearing, and it was missing from the first version of this. When the transcript carries tool results, it says so explicitly: those calls have already run, do not run them again. The protocol has no way to hand a structured tool result back outside the stream that asked for it, so by the time the model reads a completed call it is prose — and Cursor's agent is built to keep working, so a transcript ending in "I ran `ls`, here is the output" reaches it as a new turn it could reasonably satisfy by running `ls`. The maintained OpenCode plugin carries the same instruction in both of its recovery prompts, which is where the wording came from and what makes this a known failure mode rather than a hypothetical one.

## What the stateless choice actually costs

The first draft of this plan asserted that going stateless loses server-side prompt caching, and offered a live bidirectional stream as the only alternative. Both halves of that deserve correcting, because the second one is wrong and the first is only half-known.

There is a third design, and Cursor built it in. Every completed turn ends with a `conversationCheckpointUpdate` carrying the whole `ConversationStateStructure`, and `AgentRunRequest.conversation_state` takes one back — so a conversation resumes by *sending its state up with the request* rather than by holding a socket open. That is still stateless HTTP, and the state is opaque bytes a client stores wherever it likes, which means it does not put an invisible second copy of the conversation on Cursor's side. Presenting the sticky stream as the only alternative conflated two very different things and quietly buried the good one.

What it costs is real but bounded. Cursor bills the token accounting a subscription's credit pool is denominated in, and cache reads run at roughly a fifth of input rate. If resending the transcript defeats caching, input tokens — the dominant term in an agentic loop — cost about five times what they would. Whether it *does* defeat caching is not known from here: the prompt this sends is append-only, with a stable system prompt and a transcript that only grows at the end, so ordinary prefix caching should still hit most of it; but a cache scoped per conversation id would miss entirely, since every turn mints a fresh one. Nothing short of a metered account answers that.

So the position is: keep the fresh run, because it is the only design in which the transcript Daisy owns is the only transcript, and because checkpoint resume brings a blob store, an eviction policy, interrupt sanitisation and a "blob not found" failure mode along with it — all of which the maintained plugin has, and all of which exist to serve a live stream this does not have. But the checkpoint is the upgrade path if measurement says caching is being missed, and it is a much smaller step than the earlier framing implied.

One thing worth stating because it is easy to assume otherwise: **no client does anything about caching**, and there is nothing for a client to do. The service descriptor has no cache field of any kind — no `cache_control`, no ephemeral marker, nothing to mark a prefix as reusable — and none of the plugins sends anything of the sort. The one place "cache" appears in any of them is a per-model price table used to report cost, and a client-side memo of the system prompt's blob id. So caching here is entirely the server's business, and the only lever a client has over it is whether the conversation is resent or referred to. There is no cache discipline being respected elsewhere that this fails to respect.

## Where this stands against the clients that have been run

Two things were checked against them rather than assumed, and both changed the code.

The run request now matches the simpler plugin's field for field: `conversation_state`, `action`, `model_details` with the id in all three name fields, and `conversation_id`. `RequestedModel` is not sent, and that is a choice with evidence behind it — the maintained fork adds it to select a model *variant* by parameter, while the simpler plugin omits it entirely and works, and this provider addresses variants by their effort-suffixed id, which is how `GetUsableModels` already shapes them.

There is one place where following the simpler plugin would have been a mistake, and reading both is what caught it. It builds structured turns into `conversation_state.turns`, each carrying a serialized `UserMessage`. The maintained fork tore that out with a comment explaining why: the server reads `AgentConversationTurnStructure.user_message` as a blob *reference*, so turns referring to blobs a fresh conversation never stored fail with "blob not found". Its replacement is history embedded as text in the user message — which is what this does. Following the newer code here rather than the simpler code is the difference between working and not.

The client version is `cli-2026.01.09-231024f` because that is the newest string any working client actually sends, not because of what today's date is. The header set is the one paired with *this* transport rather than the union of every set seen: the plugins holding an HTTP/2 stream open for `Run` send no checksum, timezone or streaming hint, the one using `RunSSE` sends all three, and assembling a superset would invent a fourth combination nobody has run.

The gap that remains is the checkpoint, which both plugins keep per conversation behind a thirty-minute TTL and this does not keep at all. That is the subject of the section above.

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

One smaller asymmetry follows from the protocol: nothing on this provider claims image input, because the agent service takes a text turn.

## Effort is part of the model's name

Every other provider in Daisy takes a reasoning effort as a setting. Cursor puts it in the model id: `claude-4.6-opus-high`, `gpt-5.4-medium`. So this provider ignores `reasoning_effort` entirely — picking the model already said it, and a second setting could only disagree with the first.

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
