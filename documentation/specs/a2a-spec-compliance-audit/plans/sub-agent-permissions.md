# Sub-Agent Permissions and Propagated Human-in-the-Loop

This is the plan for governing what a spawned sub-agent may do and for surfacing its human-in-the-loop pauses to the user. It builds directly on [`input-required.md`](./input-required.md) — and revises the one decision made there that this supersedes: that a delegated agent hard-denies any gate. It builds on the findings in [`audit.md`](../audit.md).

## Where we are today

A spawned agent runs as a non-blocking delegated A2A task with its own fresh runtime, built from the sub-agent's configuration (`a2a_executor.py`, the `delegated` branch of `execute`). The parent's `spawn_agent` passes only a `read_only` flag through to the delegate (`agent.py`, `_run_spawned_agent` and the registry's `make_delegate`), and the sub-agent's permission mode is otherwise whatever its own configuration declares. Two gaps and one regression follow from that.

First, there is no way for the caller to set an approval policy for the sub-agent beyond `read_only`; a sub-agent runs at its own configured `permission_mode`, which could even be `bypass`. Second, an agent-card's bash permissions fall back to `allow` when no pattern matches (`configuration.py`, `evaluate_permission`), so a sub-agent in the interactive ("manual") mode silently auto-runs anything not explicitly listed rather than asking. Third, the durable input-required work made a delegated agent's gate a hard denial, because a one-shot delegated turn has no persisted checkpoint to resume from — which means a sub-agent that needs approval is refused rather than asking the user.

## The core idea

A sub-agent is not a peer of the user; it is work the user launched and remains accountable for. So its authority is the intersection of what its own card allows and what the caller granted, never more — and any decision it cannot make on its own is escalated to the user rather than guessed. Four pieces:

- A caller-set **approval policy** on `spawn_agent`, combined with the sub-agent's card default to the more restrictive of the two.
- The interactive ("manual") policy **respects the card's permissions but defaults to ask, not allow**, so nothing runs unattended just because it was unlisted.
- **`bypass` is never a legal effective policy for a sub-agent** — it is clamped to the interactive default.
- Every **human-in-the-loop gate a sub-agent raises is propagated to the user**, who approves or denies it, and the sub-agent resumes from where it paused.

## The effective policy

`spawn_agent` gains an optional `permission_mode` argument (`default` | `auto` | `read_only`; `bypass` is not accepted). The sub-agent's effective policy is computed once, at spawn, as the more restrictive of the caller's argument and the sub-agent card's own `permission_mode`, then clamped so it can never be `bypass`. Restrictiveness orders as `read_only` > `default` (ask) > `auto` > `bypass`, and the clamp maps any `bypass` — from the card or a misconfiguration — up to `default`. The existing `read_only` spawn flag still applies on top. The result rides the delegated message metadata (a new `permissionMode` key alongside the existing `readOnly`/`depth`), and the delegated runtime is set to it before the turn runs, rather than trusting the sub-agent's own configured mode.

This is enforced in one place — the executor's delegated branch — so there is no path by which a sub-agent runs at a mode looser than the caller intended or than its card allows, and none by which it runs unattended under bypass.

## Ask by default under the interactive policy

Under the `default` (manual) policy, permission evaluation keeps honoring every `allow`/`ask`/`deny` pattern the card declares, but the fallback for an unmatched command becomes `ask` instead of `allow`. A card that wants a command run unattended still says so explicitly with an `allow` pattern; silence now means ask, not allow.

This is a property of the manual policy itself, not of being a sub-agent, so it applies uniformly to the top-level agent and a sub-agent alike: `_evaluate_bash_permission` returns `ask` for an unmatched command whenever the runtime is in the interactive manual mode (not auto-classifying, not read-only, not bypass). The other modes are unaffected — `auto` self-classifies an unmatched command, `read_only` hard-blocks mutations, and `bypass` allows everything, so none of them ask on silence. Previously the top-level manual mode auto-ran an unlisted low-risk command and only prompted on medium/high risk; now manual means "ask for anything the card does not explicitly allow," at every level.

An `allow always` answer records the command as an allow rule so it stops being asked. For the top-level agent that rule is scoped to the live session. A sub-agent has no durable session to hold it, so its `allow always` is instead persisted as an `allow` pattern on the sub-agent profile's own configuration — its authority is its card, so the rule lives on the card and every future spawn of the profile inherits it (an existing `deny`/`ask` for a pattern is never overridden). The rule is also added to the live session allowlist so the rest of the current sub-agent turn stops asking without waiting for the config reload.

## Propagating the pause

The regression from the input-required work is undone: a sub-agent no longer hard-denies a gate — it raises it, and the gate reaches the user. This covers both kinds of human-in-the-loop gate a turn can raise: a permission prompt (bash/MCP/egress/sandbox approval), and an `ask_user` question — a sub-agent now carries the `ask_user` tool (it did not before, because it had no way to reach the user), so it can ask the user directly when it is blocked, rather than guessing or reporting failure back to its parent. Both propagate by the same path. (`open_artifact` stays top-level only — it drives the user's own UI, which a delegated turn does not own.) But a top-level turn and a delegated turn suspend by different mechanisms, because they have different lifecycles.

A top-level turn suspends *durably*: its preflight appends the tool-call checkpoint, emits `SUSPENDED`, and the executor persists the pending interactions and drives the task to `input-required`, to be resumed from the database by a later answer — possibly after a restart. A delegated turn cannot use that machinery: its conversation is a throwaway that is not persisted per context, and the parent that consumes its stream is a live in-process job. So a sub-agent suspends *in place*. Its runtime's preflight emits a `PERMISSION_REQUEST` (or `QUESTION`) event and then parks the turn on an in-memory future — one per gate — without closing the stream or leaving `working` state. The events carry the sub-agent's lane path and are relayed into the agents panel, where the tool line flips to "input required" so the user sees which sub-agent is asking. The actionable approve/deny (or answer) is the same shared overlay the top-level agent uses: it derives its pending prompt from both the main transcript and the agents-panel steps, so a parked sub-agent's gate raises it too — and because a sub-agent is usually still parked after the non-blocking parent turn has ended, the overlay is ungated by whether a turn is actively streaming. The answer flows through exactly the same resolve endpoint as any prompt, routed by request id.

The resolver reconciles the two paths in one place. It first looks for a durable `input-required` task that owns the request id (the top-level case); finding none, it routes the answer to the parked sub-agent by resolving the matching future on that sub-agent's still-registered participant runtime. The future completing unblocks the parked turn, which resolves the batch's decisions and drains the tools onward — so the same continuous delegation stream carries the prompt and the resumed work, and the parent's `_run_spawned_agent` consumes it as one unbroken run needing no change. A Stop while parked ends the outstanding gates fail-safe: a question declines, a permission denies.

Because a parked sub-agent never enters the durable `input-required` state and is not flagged into the durable awaiting-input marker, it stays a plain `working` task for its whole pause. That is deliberate: the marker and the input-required state are the durable top-level machinery, and coupling an ephemeral delegated pause to them would strand state that no restart can honor.

## Restart

A top-level `input-required` task is durable and resumes after a restart. A delegated pause is not, and needs no special reconciliation to be treated as such: a parked sub-agent is a `working` task, and the parent that consumes its stream is gone after a restart, so the orphaned-task pass fails it like any other interrupted background work — while an `input-required` task is the one state that pass preserves. The distinction the plan called for falls out of the state model for free: only a top-level pause is ever `input-required`. The user simply re-runs the turn, which re-spawns the sub-agent.

## Security model

The invariants are worth stating plainly. A sub-agent's effective authority is never broader than the intersection of its card and the caller's grant; `bypass` is unreachable for a sub-agent; and an unmatched command under the interactive policy asks rather than runs. Escalation is fail-safe: a gate that cannot reach a human, or a delegated pause interrupted by a restart, ends as a denial or a failure, never as a silent allow. The decision logic remains the shared functions the top-level turn uses, so a sub-agent is evaluated by exactly the same rules, only with a stricter default and a mandatory ceiling.

## Build order

1. The effective-policy computation: `spawn_agent`'s `permission_mode` argument, the more-restrictive-plus-clamp combination with the card, and threading it through `make_delegate`'s metadata into the delegated runtime.
2. Ask-by-default: the interactive-manual-mode predicate driving the `_evaluate_bash_permission` unmatched fallback (top-level and sub-agent alike), and the sub-agent `allow always` persisting to the profile's configuration.
3. Propagation: remove the delegated hard-deny (both the general gate denial and the older sandbox-read one); give the sub-agent the `ask_user` tool so it can raise a question too; park the delegated turn on in-memory futures with `PERMISSION_REQUEST`/`QUESTION` events relayed to the panel; surface the parked gate in the shared approval overlay (derived from the agents-panel steps as well as the transcript, ungated by streaming); and route the answer to the parked runtime's future in the shared resolver. `_run_spawned_agent` needs no change — the parked turn stays on its one continuous stream.
4. Restart: no work. A parked sub-agent never becomes `input-required`, so the existing orphaned-task pass — which preserves `input-required` and fails everything else nonterminal — already fails a delegated pause and preserves a top-level one.

## Testing

The effective-policy computation is a pure function and is unit-tested across the matrix of caller argument and card mode, including every `bypass` input clamping to `default`. The ask-by-default fallback is unit-tested at the configuration layer (an explicit `allow`/`deny` pattern still wins; an unmatched command resolves to `ask` only when the fallback is `ask`) and at the runtime layer (the interactive-manual predicate is true only when not auto/read-only/bypass, so an unmatched command asks in manual mode — top-level and sub-agent alike — and falls through to `allow` under auto/read-only; `set_sub_agent_policy` clamps `bypass`; and `resolve_agent_permission` drives the parked future). The `allow always` persistence is unit-tested against a real sidecar: a new pattern is added as `allow`, an existing `deny` is never overridden, unrelated config is preserved, the write is idempotent, and it is routed to the config only for a sub-agent (the top-level stays session-scoped). The propagation path is exercised against the real SDK with a delegated turn that parks and resumes through the in-memory future, confirming the sub-agent reaches completion after an approval and ends denied after a denial, with the prompt carrying the sub-agent's lane path, and that a Stop while parked ends it fail-safe. Both gate kinds are covered: a permission gate resolving to a decision, and an `ask_user` question resolving to the answers list or the decline sentinel — with `ask_user` confirmed present in a sub-agent's tool set and `open_artifact` confirmed absent. A restart test confirms a delegated pause (a `working` task) is failed while a top-level `input-required` pause is preserved.

## Open questions

- Whether the caller should also be able to pre-approve specific commands for a sub-agent at spawn (a scoped allowlist), or only set the mode. Persisting an `allow always` to the profile's card (implemented) covers the durable case; a per-spawn, non-persisted allowlist is still open.
- Whether a denied sub-agent gate should end the sub-agent or report the denial back to the sub-agent's model so it can choose an alternative — the plan denies the single action and lets the sub-agent continue, matching the top-level semantics.
- Whether a very deep delegation chain should escalate every level's gates to the one human, or collapse them; today each gate escalates independently.
