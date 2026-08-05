# Configuration reference

Every setting Frank has, in the order the settings panel presents them: what you decide first at the top, the numbers underneath everything at the bottom.

A setting is addressed by its dotted path, and the same path works everywhere — in `~/.config/frank/configuration.yaml`, in `frank configure`, and as the key the interface writes. Nothing is written to that file until you change it: a setting you have never touched follows the default, including when a release moves it.

Everything `frank configure` does:

- `frank configure` — what this machine has been set to, as one object.
- `frank configure --all` — every setting there is, each with what it ships at and what it currently runs on.
- `frank configure sandbox.enforce` — read one, printing the value and nothing else.
- `frank configure sandbox.enforce off` — change one, and print how it was stored.
- `frank configure sandbox.enforce --unset` — put it back, which removes it from the file rather than writing the default into it.

The settings panel shows the same set, with the name and the explanation in the language the interface is set to. The words there and the words here are the same words: they live in `shared/messages/`, keyed by these paths.


## Agent defaults

What a session runs under when its creator does not say.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `agent.permission_mode` | `default` / `permissive` / `classify` / `read_only` | `default` | How much a session may do without asking you, unless its agent profile caps it lower. |


## Workspaces

Where a session's tools run.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `workspace.strategy` | `none` / `branch` / `worktree` | `none` | Where a session's work happens: the project directory itself, a branch of its own, or a git worktree of its own. |


## Confinement

What a session's tool children may do, enforced by the operating system.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `sandbox.enforce` | `required` / `preferred` / `off` | `required` | What to do on a machine that cannot enforce confinement: refuse to start the session, run with resource limits only, or do not confine. |
| `sandbox.filesystem.readable` | list | `~/.agents` `~/.config` `~/.local` `~/.ssh` `~/.gitconfig` `~/.gitignore_global` `~/.cargo` `~/.rustup` `~/.npmrc` `~/.nvm` `~/.pyenv` `~/.docker` `~/.netrc` | Paths under your home a tool child may read. The system is readable and is not listed. |
| `sandbox.filesystem.writable` | list | `$WORKSPACE` `$TMPDIR` `/tmp` `$XDG_CACHE_HOME` `~/.cache` | Paths a tool child may write. Deliberately narrower than readable, because a wrong write is the failure people actually meet. |
| `sandbox.filesystem.grantable` | list | — | Paths an agent may be granted at runtime without asking. Empty means every request is put to you. |
| `sandbox.filesystem.deny` | list | `~/Documents` `~/Desktop` `~/Downloads` `~/Pictures` `~/Movies` `~/Music` `~/Library/Mail` `~/Library/Messages` `~/Library/Safari` | Paths refused outright. Wins over readable and writable, and no request opens them. |
| `sandbox.network` | boolean | `true` | Whether a tool child may reach the network at all. |
| `sandbox.limits` | map | `{'RLIMIT_CORE': 0, 'RLIMIT_FSIZE': 8589934592, 'RLIMIT_NPROC': 2048}` | Per-child resource limits, by their setrlimit names. |
| `sandbox.umask` | string | — | The file-creation mask a tool child runs under. Empty leaves the machine's own. |
| `sandbox.nice` | integer | `0` | How far to lower a tool child's scheduling priority, so a runaway command does not take the machine with it. |


## Session toolbox

Whether a session may install the tools it needs into a profile of its own.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `toolbox.enabled` | boolean | `true` | Let each session install the tools it needs into a package profile of its own, deleted with the session. |


## Permission classifier

The model call that decides a tool call nobody is there to be asked about.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `permission_classifier.reasoning_effort` | `minimal` / `low` / `medium` / `high` | `low` | How hard the judge thinks about one tool call. It weighs a command against a page of rules, which is not what an agent's own effort is for. |


## Conversation compaction

How conversation history is folded as it grows.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `compaction.automatic` | boolean | `true` | Reclaim context on its own as it fills. Manual compaction works either way. |
| `compaction.reclaim_at_fraction` | number | `0.85` | Fold once the conversation passes this share of the usable window. Late on purpose: a fold is the one thing that rewrites the prefix a provider had cached, so the fewer of them the better. |
| `compaction.condense_log_at_fraction` | number | `0.3` | Condense the observation log once the log itself passes this share. |
| `compaction.output_reserve_fraction` | number | `0.1` | Share of the window held back for the answer, so folding still has room to run. The rest is the usable window every other fraction here is measured against. |
| `compaction.recent_working_set_fraction` | number | `0.25` | Share of the usable window kept verbatim rather than folded into the log. Sized in tokens rather than turns because an unattended run is one turn and hundreds of tool results. |
| `compaction.verbatim_user_fraction` | number | `0.1` | Share of the usable window that carries the user's own messages through a fold, word for word. Their instructions are the specification, and a paraphrase of a specification is a different specification. |


## User snapshot

Whether the system prompt describes how you work on this machine.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `user_context.enabled` | boolean | `false` | Include a snapshot of how you work — your editor, your habits, your machine — in the system prompt. |


## Screen control

Driving the screen.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `computer_control.enabled` | boolean | `false` | Let the agent drive native applications and your browser. It also needs Accessibility granted in System Settings. |
| `computer_control.settle.poll_seconds` | number | `0.05` | How often to re-check whether the surface has settled. |
| `computer_control.settle.give_up_seconds` | number | `1.5` | The longest to wait before reading it anyway. |
| `computer_control.retrieval.multilingual_rank_model` | string | `minishlab/M2V_multilingual_output` | The static embedding that ranks by meaning across languages. Also the model whose plain cosine backs the relevance floor, so clearing it disables that floor. Empty turns it off. |
| `computer_control.retrieval.english_rank_model` | string | `minishlab/potion-base-32M` | A second static embedding, ranked alongside the first rather than instead of it. Stronger on queries that describe what an element is for; weaker on exact labels. Empty turns it off. |
| `computer_control.retrieval.lexical_gate_short_words` | integer | `3` | Queries of this many words or fewer are treated as a label quoted off the screen, and the character similarity counts in full. |
| `computer_control.retrieval.lexical_gate_long_words` | integer | `7` | Queries of this many words or more are treated as a description of a purpose, and the character similarity is ignored — its spelling agrees with nothing, so it ranks by coincidence. |


## Dictation

Speaking to the composer instead of typing.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `dictation.enabled` | boolean | `false` | Let the composer take speech. The model runs on this machine. |
| `dictation.model` | string | `mlx-community/parakeet-tdt-0.6b-v3` | The speech model dictation downloads and runs locally. |
| `dictation.timing.minimum_transcription_timeout_seconds` | number | `30.0` | The floor on how long one transcription may take before it is treated as wedged. |
| `dictation.timing.transcription_timeout_realtime_multiplier` | number | `0.5` | Added to the floor, per second of audio. Transcription runs comfortably faster than real time on Apple Silicon, so reaching the limit means stuck, not busy. |
| `dictation.timing.maximum_attempts` | integer | `2` | How many workers one recording may be given. |
| `dictation.timing.worker_shutdown_seconds` | number | `2.0` | How long a worker is given to exit on its own before it is killed. |


## Model providers

Credentials for each model provider. The provider's own environment variable wins over anything stored here.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `providers` | map | — | Credentials for each model provider. The provider's own environment variable wins over anything stored here. |


## Web search

The web-search backend.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `exa.api_key` | string | — | Exa API key. The EXA_API_KEY environment variable wins over this. |


## Page fetching (Jina)

The default page-fetching engine.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `jina.api_key` | string | — | Jina API key, which raises the rate limit. JINA_API_KEY wins over this. |


## Page fetching (Firecrawl)

The fallback page-fetching engine.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `firecrawl.api_key` | string | — | Firecrawl API key. FIRECRAWL_API_KEY wins over this. |
| `firecrawl.api_url` | string | — | A self-hosted Firecrawl instance to use instead of the hosted API. |


## Web fetch

Fetching a page directly.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `web_fetch.proxy_url` | string | — | Route direct fetches and file downloads through an HTTP or SOCKS proxy, for sites that block by address. Empty fetches directly. Credentials may be embedded as http://user:pass@host:port. |


## Composio

Composio's hosted MCP gateway.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `composio.enabled` | boolean | `false` | Expose Composio's hosted gateway as one MCP server. |
| `composio.url` | string | `https://connect.composio.dev/mcp` | The hosted MCP URL from the Composio dashboard's "connect" page. |
| `composio.api_key` | string | — | The API key shown beside that URL. COMPOSIO_API_KEY wins over this. |
| `composio.server_name` | string | `composio` | The MCP server name its tools appear under. |
| `composio.timeout_seconds` | number | `60` | How long one call to that gateway waits. |


## MCP servers

MCP servers, read from mcp.json.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `mcp.servers` | map | — | MCP servers, by name. Edited on the MCP panel, which is where a server's command and credentials belong. |


## Remote peers

Agents on other hosts, read from remote-agents.json.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `remote_agents.agents` | map | — | Remote peers, by name. Edited on the peers panel. |


## Telemetry

OpenTelemetry export.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `telemetry.enabled` | boolean | `false` | Export traces at all. |
| `telemetry.exporter.endpoint` | string | — | Where traces are sent. Empty sends none. |
| `telemetry.exporter.protocol` | `http/protobuf` / `grpc` | `http/protobuf` | The OTLP protocol that collector speaks. |
| `telemetry.exporter.headers` | map | — | Headers sent with every export, for a collector that authenticates. |
| `telemetry.sample_ratio` | number | `1.0` | Share of traces exported. 1.0 exports every one. |


## Tuning

How large, how many, and how patient the tools are.

| Setting | Type | Default | What it is for |
|---|---|---|---|
| `tuning.context_share.text` | number | `0.25` | Share one result's text may fill — output, fetched pages. |
| `tuning.context_share.results` | number | `0.15` | Share a set of results may fill — matches, lines, records. |
| `tuning.timeout_multiplier` | number | `1.0` | Multiplier on every wait. 2.0 doubles them for a slow machine; 1.0 is neutral. |
| `tuning.defaults` | section | — | Override one tunable by its own name, in its own unit — see frank configure --all. An override replaces the shipped default, so context_share and timeout_multiplier still apply on top. |
| `tuning.defaults.output_tokens` | integer | `16000` | Tokens of inline output one tool may return before the rest overflows to a file. |
| `tuning.defaults.fetch_tokens` | integer | `24000` | Tokens of a fetched web page's text kept inline. |
| `tuning.defaults.maximum_line_chars` | integer | `2048` | Characters of a single over-long line kept before it is clipped, so one minified blob cannot fill a result on its own. |
| `tuning.defaults.upstream_error_detail_tokens` | integer | `256` | Tokens of an upstream service's error body kept in the failure this harness raises — enough to carry the provider's own explanation, short of pasting a whole page into a message someone has to read. |
| `tuning.defaults.read_lines` | integer | `2000` | Lines a file read returns when no explicit limit is given. |
| `tuning.defaults.grep_results` | integer | `512` | Total matches one search returns. |
| `tuning.defaults.grep_per_file` | integer | `512` | Matches one search returns from any single file. |
| `tuning.defaults.glob_results` | integer | `1000` | Paths one glob returns. |
| `tuning.defaults.web_search_maximum` | integer | `10` | Ceiling on the result count a web search may ask for, however many it requests. |
| `tuning.defaults.remote_listing` | integer | `32768` | Paths listed on a remote machine before glob matching is applied locally. |
| `tuning.defaults.web_exchanges` | integer | `250` | Recent request/response pairs a browser session keeps, so a search can surface the API behind a rendered view. |
| `tuning.defaults.web_websockets` | integer | `32` | Live websockets a browser session tracks at once. |
| `tuning.defaults.web_websocket_frames` | integer | `200` | Frames retained per tracked websocket. |
| `tuning.defaults.action_timeout_ms` | integer | `5000` | How long one browser action (click, type, hover) waits for its element. |
| `tuning.defaults.navigation_timeout_ms` | integer | `20000` | How long a page load or navigation waits. |
| `tuning.defaults.snapshot_timeout_ms` | integer | `10000` | How long an accessibility snapshot of a page waits. |
| `tuning.defaults.connect_timeout_ms` | integer | `10000` | How long attaching to a browser waits. |
| `tuning.defaults.browser_authorization_ms` | integer | `90000` | How long attaching waits for the user to approve Chrome's prompt. |
| `tuning.defaults.drag_timeout_ms` | integer | `8000` | How long a drag between two elements waits. |
| `tuning.defaults.screenshot_timeout_ms` | integer | `20000` | How long capturing a page screenshot waits. |
| `tuning.defaults.read_text_timeout_ms` | integer | `10000` | How long reading a page's text waits. |
| `tuning.defaults.frame_resolve_timeout_ms` | integer | `2000` | How long resolving a frame reference waits. Deliberately well below the action timeout: a frame that has gone waits out its budget rather than erroring, and listing every frame would otherwise stall on the one that left. |
| `tuning.defaults.sigterm_grace_seconds` | number | `3.0` | How long a cancelled command or a reaped session has after SIGTERM before SIGKILL. |
| `tuning.defaults.ripgrep_seconds` | number | `30.0` | How long one content search may run. |
| `tuning.defaults.bash_sync_window_seconds` | number | `60.0` | How long a shell command runs inline before it moves to the background. It is not killed at this point, only handed off, and the model can override it per call. |
| `tuning.defaults.slow_tool_sync_window_seconds` | number | `10.0` | The same inline window for fetching a URL or downloading a file. |
| `tuning.defaults.web_search_sync_window_seconds` | number | `10.0` | The same inline window for a web search. |
| `tuning.defaults.accessibility_messaging_seconds` | number | `2.0` | How long one accessibility message to an application waits, so a hung application costs a moment rather than the whole action. |
| `tuning.defaults.goal_continuation_turns` | integer | `12` | How many turns in a row a session may open for its own goal before it stops and waits for the person. |
| `tuning.defaults.goal_blocked_turns` | integer | `3` | How many times the same condition must stop a goal before the agent may report it blocked. One failure is not an impasse, and a goal abandoned on the first refusal is one nobody asked to abandon. |
| `tuning.defaults.warm_workers` | integer | `2` | How many session workers to keep started and waiting. |
| `tuning.defaults.session_title_attempts` | integer | `3` | How many times a session asks the model to name itself before giving up. |
| `tuning.defaults.permission_classifier_attempts` | integer | `3` | How many times the permission classifier is asked before its silence counts as a refusal. |
| `tuning.defaults.prototype_start_seconds` | number | `120.0` | How long the daemon waits for the prototype to start and accept a connection. The prototype itself imports nothing heavy; it starts the workers that do, and keeps a couple of them started ahead of demand. |
| `tuning.defaults.prototype_restart_seconds` | number | `5.0` | How long the daemon waits before trying again after the prototype failed to restart. |
| `tuning.defaults.session_idle_sleep_seconds` | number | `18000.0` | How long a session keeps its process after its last turn before it sleeps. |
| `tuning.defaults.session_start_seconds` | number | `60.0` | How long the daemon waits for a forked session to bind its socket and report ready. |
| `tuning.defaults.daemon_startup_seconds` | number | `45.0` | How long a command waits for a daemon it just started to become reachable. |
| `tuning.defaults.control_plane_call_seconds` | number | `60.0` | How long one call to the daemon waits. |
| `tuning.defaults.model_catalogue_ttl_seconds` | number | `60.0` | How long the list of available models is cached. |
| `tuning.defaults.credential_refresh_leeway_seconds` | number | `300.0` | How far ahead of its expiry an access token is refreshed. |
| `tuning.defaults.daemon_probe_interval_seconds` | number | `0.05` | Pause between asks of whether another process's daemon socket answers yet, or whether a daemon being replaced has finally exited. |
| `tuning.defaults.daemon_probe_connect_seconds` | number | `0.5` | How long one connect to a daemon socket waits before it counts as unanswered. |
| `tuning.defaults.oauth_poll_interval_seconds` | number | `1.0` | First pause between asks of whether a browser sign-in has completed; it widens from here. |
| `tuning.defaults.oauth_poll_ceiling_seconds` | number | `10.0` | Ceiling on the widening pause between sign-in polls, so a slow sign-in is not asked about every second for minutes. |
| `tuning.defaults.oauth_poll_give_up_seconds` | number | `300.0` | How long a browser sign-in is waited for before it is abandoned — a person's whole trip through a consent screen, not a network round trip. |
| `tuning.defaults.subscription_resume_ttl_seconds` | number | `1800.0` | How long a subscription provider's server-side conversation state stays worth resuming from before the whole conversation is resent instead. |
| `tuning.defaults.model_silence_give_up_seconds` | number | `180.0` | How long a model may hold a stream open saying nothing at all before the turn is ended. Long, because a model weighing a hard problem is silent and still working. |
| `tuning.defaults.file_url_ttl_seconds` | number | `600.0` | How long a signed file URL stays valid. |
| `tuning.defaults.mcp_connect_seconds` | number | `20.0` | How long connecting to one MCP server waits. |
| `tuning.defaults.card_resolve_seconds` | number | `20.0` | How long fetching a remote agent's card waits. |
| `tuning.defaults.remote_command_seconds` | number | `120.0` | How long a command on another machine may run. |
| `tuning.defaults.remote_connect_seconds` | number | `16.0` | How long opening an SSH connection waits. |
| `tuning.defaults.remote_control_persist_seconds` | number | `120.0` | How long a shared SSH connection lingers after its last use, so the next command reuses it. |
| `tuning.defaults.control_script_seconds` | number | `120.0` | How long one screen-control script may run. |
| `tuning.defaults.surface_guard_margin_seconds` | number | `30.0` | How far above the script's own limit the machinery waiting on it sits, so raising that limit can never make the guard fire first and leave the surface half-dead. |
| `tuning.defaults.screencapture_seconds` | number | `15.0` | How long capturing the screen waits. |
| `tuning.defaults.open_url_seconds` | number | `5.0` | How long handing a URL to the system browser waits. |
| `tuning.defaults.type_chunk_size` | integer | `20` | Characters sent per synthesized keyboard event. |
| `tuning.defaults.drag_steps` | integer | `12` | Segments a drag is split into, so it looks like a hand moved it. |
| `tuning.defaults.scroll_amount_pixels` | integer | `300` | Pixels one scroll step moves a native window. |
| `tuning.defaults.settle_stable_reads` | integer | `2` | Identical consecutive reads that count a surface as having stopped changing. |
| `tuning.defaults.find_rephrasing_similarity` | number | `0.45` | How alike two screen queries must be, as a cosine in the retrieval model's own space, before a second one landing on the same element counts as the first asked again. |
| `tuning.defaults.find_near_weight` | number | `0.5` | How much sitting beside the anchor is worth against matching the query, when a find names one with near=. |
| `tuning.defaults.find_anchor_margin` | number | `0.02` | How far ahead of its own runner-up a near= anchor must score before a find will join on it. |
| `tuning.defaults.find_candidates` | integer | `5` | Elements find_one weighs against its best match, and offers back when it cannot choose between them. |
| `tuning.defaults.find_one_margin` | number | `0.2` | How far ahead of the runner-up find_one's best match must score, in units of the shortlist's own spread, before it answers with one element instead of asking which was meant. |
| `tuning.defaults.find_many_ceiling` | integer | `50` | Elements find_many will return however many are asked for. |
| `tuning.defaults.find_relevance_floor` | number | `0.25` | How well an element must match, as a cosine against the query, before find_many will return it at all. It cuts the noise band and nothing more. |
| `tuning.defaults.click_interval_seconds` | number | `0.01` | Pause between successive synthesized clicks. |
| `tuning.defaults.drag_step_interval_seconds` | number | `0.01` | Pause between the interpolated steps of a drag. |
| `tuning.defaults.type_chunk_interval_seconds` | number | `0.005` | Pause between typed chunks. |
| `tuning.defaults.focus_settle_seconds` | number | `0.03` | Pause after focusing a field, before typing into it. |
| `tuning.defaults.stamped_image_side` | integer | `2048` | Longest side, in pixels, of a screenshot annotated with element labels. |
| `tuning.defaults.accessibility_walk_budget_seconds` | number | `3.0` | How long one read of an app's accessibility tree may take. |
| `tuning.defaults.accessibility_ready_probe_seconds` | number | `0.4` | How long the readiness poll may spend deciding whether an app's tree has built yet. Short on purpose: it runs repeatedly while an app is still starting, and it only has to see past the window chrome. |
| `tuning.defaults.accessibility_prewarm_interval_seconds` | number | `0.4` | Pause between pre-warming the frontmost application's accessibility tree. |
| `tuning.defaults.accessibility_ready_backoff_seconds` | number | `0.2` | Ceiling on the widening pause between accessibility readiness probes. |


## Notes on individual tunables

Eleven of them carry more reasoning than a table row holds. This is where it is written down.

### `tuning.defaults.accessibility_walk_budget_seconds`

How long one read of an app's accessibility tree may take. It replaces a depth limit, which guarded the wrong quantity: a window six levels deep can take twice as long as one thirty-five levels deep, because the cost is how quickly the app answers, not how far down the answer is. Anything unread when this expires is reported as a region, so a short read says it is short.

### `tuning.defaults.find_anchor_margin`

How far ahead of its own runner-up a near= anchor must score before a find will join on it. Below this the anchor is a guess, and organising a ranking around a guess is worse than not anchoring: it catches a third of the failures for one correct answer in 242.

### `tuning.defaults.find_many_ceiling`

Elements find_many will return however many are asked for. There used to be an `all=True` that bypassed the limit entirely and returned the whole ranking; on one ordinary page that was 590 elements and 1.5MB, which ended the turn by exceeding the model's context window. A find is a ranked search, and the tail of a ranking is not more answer.

### `tuning.defaults.find_near_weight`

How much sitting beside the anchor is worth against matching the query, when a find names one with ``near=``. Measured over 284 anchored cases on ten applications: relevance alone answers 20.8% of them and proximity alone 21.5%, while the two together answer 85.2%. Neither half carries this on its own.

### `tuning.defaults.find_one_margin`

How far ahead of the runner-up `find_one`'s best match must score, in units of the shortlist's own spread, before it answers with one element instead of asking which was meant.

Measured over 13,628 queries on 50 recordings: at 0.20 it asks on 15.0% of calls and takes precision from 54.0% to 58.5%, deferring 8.0% of the answers that were right. The curve is smooth and the choice is a budget rather than a discovery — 0.10 asks on 10.3% for 57.2%, 0.30 asks on 19.5% for 59.7%, 0.60 asks on 30.4% for 62.6%. Deferring is cheap because the answer is usually still in the shortlist it hands back: recall at eight among deferred calls is 65.5%.

Both the number and the quantity changed with the ranker. It used to be a fraction of the top score, which is meaningful for a cosine and not for a sum of standardised signals, where the score is comparable within one ranking and arbitrary between two. Re-fit this whenever the ranking changes rather than carrying the number across — the previous note said so about the key, and the ranker is the larger change.

### `tuning.defaults.find_relevance_floor`

How well an element must match, as a cosine against the query, before `find_many` will return it at all. It cuts the noise band and nothing more.

The limit of that is measured rather than assumed. On 596 elements of a real page, six paraphrased queries for things that WERE present scored 0.48 to 0.75 at top-1, while six for things that were plainly absent — a checkout button, a flight time — scored 0.26 to 0.59. Those distributions overlap, so no absolute cosine separates "here" from "not here" with this embedding. Set it where it removes the tail that is unambiguously noise (that page ran to -0.12) without touching the band where real matches live.

Treat an empty result as "nothing scored above the noise", never as proof of absence, and do not raise this hoping to buy absence detection — it would cost real matches first.

That claim has since been tested properly and it holds, which is worth recording because an easier test says otherwise. Against negatives drawn from unrelated screens the cosine looks like it separates present from absent well, at AUC 0.94 — but a query about a periodic table asked of the Finder is not the case a floor exists for. Against the real one — 12,304 queries written for a screen and then asked of that same screen with their own element removed — it is AUC 0.55, and a threshold refusing 90% of them would discard 84% of the answers that were present.

It stays a cosine against the query, deliberately, now that the ranking is a fusion. A fused score is scaled by the query's own length, so absent queries can outscore present ones on it (AUC 0.07) purely by being shorter. A floor has to read a number that means the same thing from one call to the next, and only the plain cosine does.

### `tuning.defaults.find_rephrasing_similarity`

How alike two screen queries must be, as a cosine in the retrieval model's own space, before a second one landing on the same element counts as the first asked again. Both halves are required: across 127 rephrasing sequences and 113 legitimate ones, likeness alone caught everything and cried wolf on 76% of honest work, while the same element reached from three wordings never cried wolf but missed 12% and noticed a call and a half later. Together: everything caught, 4% false, and noticed by the second query.

### `tuning.defaults.frame_resolve_timeout_ms`

How long resolving a frame reference waits. Deliberately well below the action timeout: a frame that has gone waits out its budget rather than erroring, and listing every frame would otherwise stall on the one that left.

### `tuning.defaults.prototype_start_seconds`

How long the daemon waits for the prototype to start and accept a connection. The prototype itself imports nothing heavy; it starts the workers that do, and keeps a couple of them started ahead of demand.

### `tuning.defaults.session_idle_sleep_seconds`

How long a session keeps its process after its last turn before it sleeps. Five hours by default: long enough that a working day of on-and-off use never pays a wake, short enough that a machine left overnight is not holding interpreters for conversations nobody returned to.

### `tuning.defaults.warm_workers`

How many session workers to keep started and waiting. A worker spends about two and a half seconds importing the runtime before it can serve, and none of that work depends on which session it becomes — so some are started ahead of demand and a new session is handed one instead of waiting. Raise it if several sessions are often created at once. One is the minimum: a session created with the pool empty starts its own worker and waits for the import, which is what every session did before the pool existed.
