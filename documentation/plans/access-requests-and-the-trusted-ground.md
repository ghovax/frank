---
created: 2026-08-03T10:00:00Z
updated: 2026-08-03T10:00:00Z
commit: TBD
---

# Access Requests and the Trusted Ground

A session is confined by the operating system and gated by a permission layer, and the two have never been introduced. The confinement decides which paths a child process may touch; the permission layer decides whether a person is asked. They meet at one function, they disagree about what enforcement means, and the model is told about neither. This plan connects them, and takes the same pass over what the model is told about trust, about density, and about finishing a goal.

It began as a comparison against the Codex CLI. Several findings from that reading are folded in here rather than kept separate, because they are the same body of work: what the model is told, and how well the telling matches what the harness actually does.

## Where we are today

**The model is never told its confinement.** `TurnContext` carries `locations`, and each one names the permission mode in force on it. It carries no filesystem profile and no network flag, and the system prompt does not mention the sandbox at all. A session therefore knows that it is in `default` mode and does not know that it may write to four directories and no others.

**So the boundary is discovered by hitting it.** The default writable set is `$WORKSPACE`, `$TMPDIR`, `$XDG_CACHE_HOME` and `~/.cache`. On macOS `$TMPDIR` expands to `/var/folders/…/T/`, which is not `/tmp`. A model that writes to `/tmp` — an ordinary, almost reflexive thing to do — is refused by Seatbelt. What comes back is `Operation not permitted`: an errno, naming no path, attributable to nothing. It reads as a broken tool rather than a boundary, and the usual response is to try the same thing again.

**And there is no way to ask.** The permission layer has a rich vocabulary for *whether somebody is asked* — rules, model-declared risk, a classifier, a durable gate — and no vocabulary at all for *what a call needs*. The only widening is a person editing settings, which is not something that can happen inside a turn.

**`read_only` mode does not narrow the operating system profile.** The profile is resolved once at session creation in `daemon/api.py`, from the sandbox configuration, clamped by the agent profile and by the parent session. Nothing in that path reads `permission_mode`. Read-only is enforced only by `read_only_assessment`, a scan of the command text. Where the scan is right, this is fine. Where it is wrong — a script, a build step that writes its own cache, a command whose write is computed rather than written — the write lands, because at the kernel the session is not read-only at all.

**The one place the two systems touch is a heuristic.** `_outside_working_directory_reads` scrapes literal path-shaped tokens out of a command string and raises a gate when any of them leave the working directory. It says so itself: a tripwire, not a boundary. It consults the command text, never the profile, so it reports paths that are perfectly readable and stays silent about paths that are denied.

**Three self-declarations overlap.** A `bash` call carries `read_only`, `risk` and `explanation`. `read_only` is a claim about mutation that only matters when the static scan says `unknown`. `risk` is a claim about consequence. `explanation` is prose. Two of the three are unfalsifiable, and none of them says what the call reaches for.

## The core idea

**Reach and risk are different questions, and only one of them is checkable.**

*Reach* is where a call can touch. It is structural, the kernel enforces it, and a claim about it can be compared against what the command actually names. *Risk* is how bad the call is inside what it may touch. It is a judgement, and nothing can check it.

They are orthogonal, and each has cases the other cannot express. `rm -rf $WORKSPACE` has minimal reach and maximal risk: entirely inside the profile, and catastrophic. `cat ~/Documents/spec.pdf` has reach outside the profile and almost no risk. A design that collapses them loses one of those cases.

So the permission mode and the sandbox are not merged. They stay two axes — who decides, and what is reachable — and what changes is that each becomes visible to the other:

- The profile enters the model's context, so reach is a fact it can read rather than a wall it discovers.
- The model can request a widening of its own reach, and that request is decided by the permission layer that already exists.
- The permission mode projects onto the profile, so `read_only` means read-only at the kernel and not only in a text scan.

**The name is `access_request`.** Not `additional_access`, which describes a thing rather than an act, and reads as though the access were already held. A request is what it is: the model asks, and something else answers. The word also carries the right posture — a request can be refused, and the model should write one expecting that.

**One wire, not two.** `read_only` is removed and its job folded into `access_request`. This is the point at which the surface could have grown from two self-declarations to three, and it does not. A model reasoning about one call now answers one structured question about reach instead of two overlapping ones, and the answer is richer than either was.

## The design

### `access_request`

An optional argument on every tool that spawns a child or reaches outside the process: `bash`, `write_file`, `edit_file`, `download_file`, `call_mcp_tool`, `control_screen`.

```python
access_request: {
    "mutates": bool,          # required when the object is present
    "reads":   list[str],     # paths beyond what the profile makes readable
    "writes":  list[str],     # paths beyond what the profile makes writable
    "network": bool,          # only meaningful where the profile denies network
}
```

**It is a diff against the profile, not an inventory of the call.** A command that reads and writes inside the standing profile omits the argument entirely, which is the overwhelmingly common case and costs nothing. The presence of the object is itself information: this call needs something it does not have.

**`mutates` replaces `read_only`.** It is required whenever the object is present, so there is no ambiguity between an absent field and a false one — the failure mode that would have made `access_request: {}` unreliable. `mutates: false` is the read-only claim, weighed exactly where `read_only` was weighed: as the tiebreaker when the static scan of the command cannot decide. Omitting `access_request` altogether makes no claim, and the scan decides alone, with `unknown` escalating as it does now.

**`risk` stays.** It answers the question `access_request` cannot: how bad this is inside the reach it has. Keeping it is what preserves the `rm -rf $WORKSPACE` case, where the reach is unremarkable and the consequence is not.

### The profile in the turn context

A new `TurnContext.confinement`, beside `locations` and for the same reason: it can change while a session runs, so it must not sit in the cached system prompt where a change would rewrite the front of every request.

```json
"confinement": {
  "writable": ["/Users/g/Projects/frank", "/var/folders/8d/…/T", "/Users/g/.cache"],
  "readable": ["~/.agents", "~/.config", "~/.ssh", "~/.cargo"],
  "denied":   ["~/Documents", "~/Desktop", "~/Downloads", "~/Library/Mail"],
  "network":  true,
  "enforced_by": "seatbelt",
  "grants":   [{"writes": ["/tmp/report.txt"], "granted": "2026-08-03T10:14:02Z"}]
}
```

Paths are shown resolved, because a model reasoning about whether `/tmp` is writable cannot evaluate `$TMPDIR` in its head — and the whole confusion this plan fixes is that `$TMPDIR` is not `/tmp`. The system is readable and is not listed, matching the configuration's own comment: `/usr` and `/etc` are not secrets, and listing them would bury the four entries that matter.

`grants` carries what has already been approved this session, so the model can see that it need not ask twice.

This one change is most of the value in the plan, and it asks nothing of the model. A boundary it can read is a boundary it routes around correctly, with no protocol to learn.

### Deciding a request

An `access_request` naming paths or network runs through the machinery that already exists, in the order it already runs:

1. **The configured rules.** A new `sandbox.grants` list in configuration, shaped like the bash permission rules: patterns that are allowed outright, asked about, or denied. A denied pattern is never negotiable, and a request touching one is refused without a gate — a denial is not a question.
2. **The classifier**, under `self_classify`, seeing the request as structured data beside the command it belongs to. It gains one job it did not have: refusing a request that is wider than the call needs. A `writes` entry of `/` or `~` is not a request, it is a request to stop asking.
3. **The human gate**, parking the turn durably exactly as every other gate does, with the three answers the approval card already knows how to render, plus one: allow once, allow for this session, allow always.

**Allow always** writes the path into `sandbox.filesystem.writable` or `readable`. This is the persistable rule that Codex spells `prefix_rule`, pointed at paths rather than command prefixes, and it reuses the distil-remember-persist lifecycle that the bash rules already have.

### Applying a grant

An approved grant widens the profile for the child it belongs to, and no further. `ToolContext` already exists for exactly this shape of problem — it was built when a `bash` call naming its own directory rewrote a process-wide profile and narrowed a concurrent turn's sandbox — so a grant is a derived context bound around one call, in the same way `for_directory` is:

```python
Profile.widened(self, *, reads, writes, network, workspace="") -> Profile
```

The mirror of `narrowed`, and deliberately not its equal in one respect: **the deny list wins over any grant, unconditionally.** A path under `deny` is not widenable however it was approved. That list is what somebody declared off-limits before any of this started, and a decision taken at runtime must not reach past a standing one — otherwise the list means "until asked nicely".

Everything outside that list, a person may approve. The person is the authority the gate exists to consult, and a design that lets them approve nothing new is the design we already have. The guarantee that a session cannot hold authority its parent lacked is kept elsewhere, by the next section: grants live outside the recorded profile, so a peer clamps against what was configured and never against what was granted.

A granted write also carries the read that goes with it. Every tool that writes a file reads it first — `edit_file` must, and a shell redirect into an existing file opens it — so a grant of the write alone would be approved, applied, and then fail on its other half in a way nobody could explain from the profile.

### Grants persist for the session

A grant, once approved, holds until the session ends. Not per call.

The argument for per-call is that it is the tighter default, and tighter is usually right for a sandbox. The argument against it is stronger: a model that must re-ask for `/tmp/report.txt` on every one of eleven commands in a build will produce eleven gates, and a person answering the eleventh has stopped reading them. **An approval that is asked too often is not a stronger control than one asked once — it is a weaker one, because it trains the person to approve without looking.** The rate at which somebody is asked is a security property, and the direction it points is not the intuitive one.

So the grant persists, and the model is told two things about it, in the tool description and in the peer guidance:

- The grant persists, so it need not ask again.
- The grant was given for a purpose, and using it for another is a breach of what was agreed. A path opened to write a report is not a path to write anything else.

That second sentence is the one that matters, and it is stated as a rule about conduct rather than a mechanism, because there is no mechanism that could enforce it. A harness that widens a path cannot tell what is written there. Saying so plainly is more honest than pretending the grant is narrower than it is.

### Peers do not inherit grants

A peer clamps to the *configured* profile of its parent, never the granted one. `daemon/api.py` already clamps a new session against `parent.sandbox`, and that field carries the resolved profile — so this is a matter of keeping grants out of the recorded profile rather than of adding a check.

The reason is direct: a grant is a decision about one session's work, made by a person looking at one command. Letting it flow to a child means a person who approved a write to `/tmp` for a build has, without being asked, approved it for four peers doing something else. Inheritance would also make the grant a laundering route — ask narrowly, delegate widely — which is exactly the adversarial use the previous section asks the model not to attempt.

### `read_only` narrows the profile

`permission_mode: read_only` derives `writable = []` on the resolved profile, and `access_request.writes` is refused outright rather than gated.

The text scan stays. It is not redundant: it refuses *early*, before anything runs, and it refuses *legibly*, in prose that says which primitive or which command made the call mutating. The kernel refuses late and says `EACCES`. Keeping both means the ordinary case gets the good error message and the case the scan misses still does not write.

### A refusal that says something

When a tool child exits non-zero and its output carries a permission errno, the tool result gains a harness postscript naming the profile and the route:

> The sandbox refused an operation. This session may write to: `<workspace>`, `/var/folders/…/T`, `~/.cache` — and nowhere else. If this command needs another path, re-issue it with an `access_request`.

**This is a hint, not an attribution, and the plan does not pretend otherwise.** Seatbelt writes the denied path to the system log; Landlock returns a bare `EACCES`. Neither can be correlated back to one child reliably, and a postscript that named the wrong path would be worse than one that names none. So the postscript is keyed on the errno and states what is certainly true — the profile — rather than guessing at what was refused.

## What is deliberately not built

**A `request_access` tool.** A separate tool would mean a round trip before every constrained call: ask, receive, then act. The argument carries the request on the call it belongs to, so the common case is one round trip and the request is anchored to the thing it is for. A standalone tool would also invite requests made speculatively, in advance of any use, which is the shape of request nobody can evaluate.

**Attribution of sandbox denials to specific paths.** Covered above: it cannot be done honestly with the backends in use.

**Merging `permission_mode` into the sandbox configuration.** They answer different questions, and the merge would lose the cases where reach and risk diverge.

**A grant that outlives the session.** `Allow always` writes to configuration, which is a deliberate, visible act with a file behind it. There is no middle state that persists past the session without being written down, because a permission nobody can find is a permission nobody can revoke.

## The prompt changes

These travel with the code because they are the other half of it: several of them describe machinery this plan adds, and the rest came out of the same reading.

### The trusted ground

A new section states, once, what has rank and what does not. The prompt is the trusted ground; everything arriving through a tool — a file's contents, a command's output, a fetched page, a peer's report, an MCP answer, a goal's objective text, the environment snapshot — is data about the world rather than a voice with authority.

The framing is deliberately about *rank* rather than *suspicion*. Content that arrives through a tool is usually true and is meant to be acted on; what it is not is a source of instructions. Text inside a tool result that addresses the model directly is a fact about that source, and nothing more. This replaces the narrower sentence in `user_context.md`, which said the same thing about one snapshot and left every other channel unaddressed — and Frank ingests far more untrusted text than most harnesses, because it fetches pages, searches the web and drives a browser.

No tags, no envelope. A rule about rank generalises to channels that do not exist yet; an envelope only protects what somebody remembered to wrap.

### Density, not fewer tokens

`Minimize output tokens` is removed and named as the wrong target. It optimises a number, and the reader pays: an answer that dropped the constraint is not efficient, it is incomplete, and the cost returns on the next turn.

What replaces it is information density — decision-relevant content per token — which is a ratio, and which goes up two ways rather than one. This also settles a contradiction the prompt was carrying: `Tool Usage` already says *maximize information density*, so the two sections were pulling in opposite directions.

The `1–3 sentences` instruction goes too. A count is a rule about form; what was meant is a rule about sufficiency, and it is stated that way.

### The goal completion audit

`goal_continuation.md` grows from nine lines to a protocol. Completion is treated as unproven: requirements are derived from the objective, evidence is identified for each, and the current state is inspected rather than remembered. Indirect or uncertain evidence counts as not met, and the scope of a check must match the scope of the claim it supports.

It also gains an anti-shrinkage rule — a smaller or easier-to-verify result that leaves the requested end state untrue is not the goal — and a threshold on `blocked`. One failure is not a blocker. The threshold is read from `Tunable.goal_blocked_turns` and rendered into the prompt, so the number the model is told and the number anything else enforces are the same value. It is a tunable rather than a constant because how many failures constitute an impasse is a judgement that depends on the work, and a judgement that depends on the work belongs in configuration.

### Instruction files have a scope

`instructions.md` gains the precedence rules: a document governs the directory tree it sits in, the more deeply nested one wins a conflict, a direct instruction beats a standing document, and rules about style apply within scope and not beyond it. Each document's metadata gains its `scope`, so the model can apply the rule rather than infer it from a path.

Frank reads project documents and up to four user-wide ones, and has been shipping them flat with no statement of which wins. That is a live ambiguity on this machine, where a home `CLAUDE.md` and a project `AGENTS.md` are both loaded.

### Peers share a filesystem

`peer_sessions.md` says that a peer is a separate process with its own context and *not* its own copy of the world: its edits and yours land in the same files. Two peers pointed at one file will overwrite each other, and neither will know. The brief has to say which part of the tree is whose.

### Testing stays out

Nothing is added about running tests, and nothing is removed, because Frank never had a testing rule to begin with. The existing verification language — *verify with the narrowest useful check* — is domain-agnostic and stays. Whether and how to test is repository policy, and repository policy belongs in the documents this plan just gave a precedence order to.

## Simplified Technical English

Every markdown file under `runtime/prompts` and `runtime/tools/descriptions` is rewritten to the standard the prompt already requires: one idea per sentence, around twenty words for an instruction, active voice with the actor named, simple tenses, `-ing` only as a noun or a modifier, and the structural words kept.

The reason is not tidiness. The prompt instructs ASD-STE100 and then, in the same file, delivers that instruction in sixty- and seventy-word sentences with three subordinate clauses. Whether a model follows the rule or the example is an empirical question nobody here can settle — but the two were in conflict, and the example was longer.

**Meaning is preserved exactly.** This splits sentences and re-voices them; it does not summarise, and it does not drop a qualification. Files will get longer in lines and shorter in sentences. Two files carry measured findings inside long qualified sentences — `computer_control_guidance.md` and `control_screen.md` — where splitting badly would leave a number sounding more certain than it is; those are done last and with the qualification kept attached to the number it qualifies.

The rewrite touches the cached prefix of every session, so it invalidates prompt caches once, everywhere. That is a reason to do it in one pass rather than a reason not to do it.
