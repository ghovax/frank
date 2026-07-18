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

Under the `default` (manual) policy, a sub-agent's permission evaluation keeps honoring every `allow`/`ask`/`deny` pattern the card declares, but the fallback for an unmatched command becomes `ask` instead of `allow`. Concretely, the runtime carries an "ask when unmatched" disposition for a sub-agent in the interactive mode, and `_evaluate_bash_permission` returns `ask` where it would otherwise have fallen through to `allow`. A card that wants a sub-agent to run a specific command unattended still says so explicitly with an `allow` pattern; silence now means ask, not allow. The top-level agent's behavior is unchanged — this disposition is set only for sub-agents.

## Propagating the pause

The regression from the input-required work is undone: a sub-agent no longer hard-denies a gate — it raises it, and the gate reaches the user. The sub-agent uses the same durable segment machinery as any turn: its runtime's preflight suspends the turn at the tool-call checkpoint and emits `SUSPENDED`, and the executor drives the sub-agent's task to `input-required` carrying the pending interactions.

Two things make that visible and resumable for a delegated turn specifically. First, the sub-agent's events — including the permission prompt — already carry its lane path and are relayed into the agents panel, so the user sees which sub-agent is asking and answers it there through the same resolve path as any prompt; the resolver finds the sub-agent's task by request id and routes the answer to that sub-agent's handler. Second, because a delegated turn's conversation is a throwaway that is not persisted per context, the executor keeps the paused sub-agent's runtime in an in-process cache keyed by its task id, and the resume drives that same live runtime rather than rebuilding from the database. The parent's `_run_spawned_agent` no longer treats the sub-agent's `input-required` as an end-of-work; it waits for the sub-agent to reach a genuine terminal state — across as many approve/deny pauses as the turn takes — before injecting the deliverable.

## Restart

A top-level `input-required` task is durable and resumes after a restart. A delegated one is not: its resume depends on the in-process runtime cache, and the parent that launched it is itself gone after a restart, so there is nothing to deliver a resumed result to. A delegated `input-required` task is therefore failed on restart like any other interrupted background work, rather than preserved — the orphaned-task reconciliation distinguishes a top-level pause (preserve) from a delegated one (fail). The user simply re-runs the turn, which re-spawns the sub-agent.

## Security model

The invariants are worth stating plainly. A sub-agent's effective authority is never broader than the intersection of its card and the caller's grant; `bypass` is unreachable for a sub-agent; and an unmatched command under the interactive policy asks rather than runs. Escalation is fail-safe: a gate that cannot reach a human, or a delegated pause interrupted by a restart, ends as a denial or a failure, never as a silent allow. The decision logic remains the shared functions the top-level turn uses, so a sub-agent is evaluated by exactly the same rules, only with a stricter default and a mandatory ceiling.

## Build order

1. The effective-policy computation: `spawn_agent`'s `permission_mode` argument, the more-restrictive-plus-clamp combination with the card, and threading it through `make_delegate`'s metadata into the delegated runtime.
2. Ask-by-default: the interactive-mode disposition for sub-agents and the `_evaluate_bash_permission` fallback change.
3. Propagation: remove the delegated hard-deny; the in-process runtime cache for a paused delegated task; the resume routing; and `_run_spawned_agent` waiting for a genuine terminal across pauses.
4. Restart: distinguish top-level from delegated `input-required` in the orphaned-task reconciliation.

## Testing

The effective-policy computation is a pure function and is unit-tested across the matrix of caller argument and card mode, including every `bypass` input clamping to `default` and the interactive fallback resolving to `ask`. The propagation path is exercised against the real SDK with a delegated turn that suspends and resumes through the in-process cache, confirming the sub-agent reaches completion after an approval and ends denied after a denial, with the prompt carrying the sub-agent's lane path. A restart test confirms a delegated pause is failed while a top-level pause is preserved.

## Open questions

- Whether the caller should also be able to pre-approve specific commands for a sub-agent at spawn (a scoped allowlist), or only set the mode.
- Whether a denied sub-agent gate should end the sub-agent or report the denial back to the sub-agent's model so it can choose an alternative — the plan denies the single action and lets the sub-agent continue, matching the top-level semantics.
- Whether a very deep delegation chain should escalate every level's gates to the one human, or collapse them; today each gate escalates independently.
