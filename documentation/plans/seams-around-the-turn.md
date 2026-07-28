---
created: 2026-07-28T21:15:00Z
updated: 2026-07-28T21:15:00Z
commit: TBD
---

# Seams Around the Turn

Everything durable in a library session is already a seam. The model, the checkpoints, the jobs, the transcript, the observer, the approver, the sandbox, the catalogue, the peers — each is a constructor argument with a `typing.Protocol` behind it, and each defaults to something a library may safely do. That work made a session's *state* and its *I/O* fully substitutable. It left its *behaviour* fixed: what happens between the model call and the tool result is one composition of four mixins, and a program embedding the harness can observe none of it and change none of it. This plan adds three seams around the turn — hooks, a tool pipeline, and compaction — and deliberately stops short of a fourth.

## Where we are today

**Compaction is one strategy with three dials.** `_should_compact` reads `compaction.auto`, `observer_context_fraction` and the live context window off the mixin, and `compact()` runs an Observer/Reflector pass that calls the model twice. That is a good default and a defensible one. It is also the *only* one: a program that wants "keep the last twenty turns and drop the rest" — deterministic, free, and correct for a scripted agent that has no long-horizon memory to preserve — has no way to say so. The fractions can be nudged; the strategy cannot be replaced.

**A tool call has no seam around it.** `tools=` adds tools. Nothing wraps them. There is no place to time a call, retry a flaky one, cache an expensive one, or record what the agent actually reached for, and the only way to get any of that today is to build it inside every tool — which means a caller's own tools can have it and the harness's built-ins never can. The asymmetry is the tell: a cross-cutting concern that can only be applied to half the tools is not implemented, it is worked around.

**A turn cannot be bounded or watched.** There is no iteration ceiling and no stuck-detector, by design — the model owns progress and ends its own turn. That is right for a person at a keyboard who can hit Stop. It is wrong for a scheduled job with a budget, where "runs until the model decides" is not a policy anyone chose. And a program that wants to audit every prompt before it leaves the process has no interception point at all.

**The name says the wrong thing twice.** `turnloop.py` names the mechanism rather than the subject, and `_bound_llm` names the vendor category rather than the thing. Both are small, both are load-bearing to how the file reads, and both are fixed here because this plan touches every site that uses them.

## The core idea

**Three seams, each at a point the loop already has, and each additive.** The loop already calls the model, already resolves a batch of tool calls, already executes them, and already decides whether to fold history. Those four moments are the seams. Nothing new is invented; what exists becomes nameable and replaceable.

**A hook sees a turn and may bound it. It cannot replace the loop.** `TurnHook` has three optional methods — `before_model`, `before_tools`, `after_turn` — and a hook implements only the point it cares about. `before_tools` is the one that carries weight: it receives the batch *after* the permission barrier has resolved it, so a hook may return fewer calls but never a call the rules denied. That ordering is the security-relevant line in this plan, and it is why a cap is expressible as a hook at all.

**The cap ships as an implementation of the seam, not as a parameter.** `MaximumToolCalls(20)` is twelve lines of ordinary code with no privileges, and it is in the box because a seam whose first user is the harness itself is a seam of the right shape. Had it been `maximum_tool_calls=20`, the loop would be hardcoding one policy and the next policy would need another argument.

**Middleware wraps one call, and composes.** `ToolMiddleware.run(call, proceed)` is the standard shape — `proceed` is the rest of the pipeline — so ordering is explicit at the call site and each layer is testable alone. It wraps every tool, the caller's and the harness's alike, which is the asymmetry above resolved.

**Compaction becomes a protocol, and today's behaviour becomes its default implementation.** `ObserverReflector` is extracted from the mixin and passed the state it needs rather than reaching for it, which is what makes an alternative possible at all. `KeepRecentTurns` ships beside it as the cheap answer.

## What is deliberately not built

**The turn loop itself stays fixed.** A `runtime=` seam is twenty lines to specify and would let a caller supply a working agent loop — and that loop would silently drop the permission preflight, the durable suspend and resume, concurrent batch execution, compaction, abort and steering. A caller who reimplements those has copied `turnloop.py`; a caller who does not has a harness that runs tools without asking. The seam is not withheld for effort: `session.runtime` is already public, and a program that genuinely wants a different architecture can drive `AgentRuntime` directly or not use `Session` at all. What a `runtime=` argument would add is a door that looks supported onto a room where the safety properties do not hold.

## The design

### `TurnHook`

```python
class TurnHook(Protocol):
    """Sees a turn as it runs, and may bound it. It cannot replace the loop.

    Every method is optional; implement only the point you care about. A hook that raises
    is logged and skipped, because a turn must not fail on account of something watching it.
    """

    async def before_model(self, messages: list) -> list:
        """The conversation about to go to the model. Return it, or a changed copy."""

    async def before_tools(self, calls: list[dict]) -> list[dict]:
        """The batch about to run, as the permission barrier resolved it. Return it, a
        subset, or an empty list. A hook narrows; it can never widen."""

    async def after_turn(self, summary: TurnSummary) -> None:
        """The turn is over: what it did, what it cost, how it ended."""
```

```python
class MaximumToolCalls:
    """Stop a turn from making more than `maximum` tool calls."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._used = 0

    async def before_tools(self, calls):
        remaining = max(0, self._maximum - self._used)
        self._used += min(len(calls), remaining)
        return calls[:remaining]
```

### `ToolMiddleware`

```python
class ToolMiddleware(Protocol):
    """Wraps one tool call. `proceed` is the rest of the pipeline."""

    async def run(self, call: ToolCall, proceed: Callable[[ToolCall], Awaitable[Any]]) -> Any: ...
```

```python
class Timed:
    async def run(self, call, proceed):
        started = time.monotonic()
        try:
            return await proceed(call)
        finally:
            metrics.timing("frank.tool", time.monotonic() - started, tags={"tool": call.name})
```

### `Compaction`

```python
class Compaction(Protocol):
    """Decides when a conversation is folded, and how."""

    def should_compact(self, state: CompactionState) -> bool: ...
    async def compact(self, state: CompactionState) -> list: ...
```

```python
class KeepRecentTurns:
    """Drop old turns rather than summarising them. No model call, no cost."""

    def __init__(self, keep: int = 20) -> None:
        self._keep = keep

    def should_compact(self, state) -> bool:
        return len(state.messages) > self._keep * 2

    async def compact(self, state):
        return state.messages[-self._keep * 2:]
```

### At the call site

```python
async with Session(
    reviewer,
    directory="/srv/checkout",
    hooks=[MaximumToolCalls(20), AuditPrompts()],
    pipeline=[Timed(), RetryTransient()],
    compaction=KeepRecentTurns(20),
) as session:
    ...
```

All three default to what happens today: `hooks=()`, `pipeline=()`, and `compaction=None` meaning the `ObserverReflector` built from configuration.

## Where each seam attaches

| Seam | File and point | Fires |
|---|---|---|
| `before_model` | `turnloop.py`, before `astream(messages)` | Every model call |
| `before_tools` | `turnloop.py`, between `_resolve_tool_decisions` and `_drain_tools_concurrently` | Every batch, after the barrier |
| `after_turn` | `turnloop.py`, where `Done` is yielded | Once per turn |
| Pipeline | `_drain_tools_concurrently`, around each call | Every tool call |
| Compaction | `compaction.py`, `_should_compact` and `compact` | When the loop asks |

## The renames

`turnloop.py` becomes `turn.py`: the file is about a turn, and "loop" is how it is implemented rather than what it is. `_bound_llm` becomes `_bound_model` throughout, because the object is a model and "LLM" is a category the rest of the codebase does not use in identifiers. Both are mechanical and both are done here because this plan already touches every call site.

## Verification

Five of the six checks need no model, which matters: the account this is developed against has reached its usage limit, and a plan that can only be verified by spending quota is a plan that goes unverified.

| # | Check | Kind |
|---|---|---|
| 1 | `MaximumToolCalls(2)` given five calls returns two | Pure |
| 2 | A hook that raises does not fail the turn | Pure |
| 3 | `KeepRecentTurns(3)` on twenty messages returns six | Pure |
| 4 | `[A, B]` runs `A → B → tool` | Pure |
| 5 | Passing none of the three behaves exactly as today | Pure |
| 6 | `ruff`, `tsc`, `e2e/smoke.mjs` | Integration |

## Risk

**The compaction extraction is the only part that can break something that works today.** `ObserverReflector` currently reaches for `self._global_configuration`, `self._context_window` and `self._latest_context_tokens`; extracting it means defining `CompactionState` to carry those explicitly, and an omission there is a behaviour change rather than a compile error. It is therefore done last, after the two purely additive seams have landed, and check 5 is aimed squarely at it.

The other two seams cannot regress a caller who does not use them: an empty hook list and an empty pipeline are both a no-op branch.
