# Daisy as a library

The harness runs turns. The daemon makes those turns addressable, durable and crash-isolated.
Those are separable, and this is the half without the daemon: `daisy.Session` drives an agent
in your own process.

```python
import asyncio
from daisy import Session

async def main() -> None:
    async with Session("general-assistant", directory=".") as session:
        print(await session.ask("what does this project do?"))

asyncio.run(main())
```

Use it to embed the harness in another program, to write a terminal interface that shares code
with the browser one rather than reimplementing it, or to run a one-shot agent in a script.

## What you give up

A library session is an object, not a process. It has none of the three properties `daisyd`
exists to provide:

| Property | Library | Daemon |
|---|---|---|
| Addressable from outside | No | Yes — a socket, a token, `daisy send` |
| Outlives the program that made it | No | Yes |
| Crash-isolated | No — a tool that exhausts memory takes you with it | Yes — one process per session |
| Peers (`create_session` and friends) | Only if you supply `peers` | Yes |
| Confinement of tool children | **Identical** | **Identical** |

Confinement is the one that surprises people, so it is worth stating plainly: a session
process was never sandboxed, its *tool children* are, and a child is confined at the moment it
is spawned. That is the same code on both paths.

## The seams

Everything durable is a constructor argument with an interface behind it. The defaults are
chosen for a program that is not a daemon, which for anything durable means *in memory* — a
library that writes a database into your home directory because you ran one background command
is a library you cannot embed.

| Argument | Interface | Default | What it decides |
|---|---|---|---|
| `model` | LangChain [`BaseChatModel`](https://python.langchain.com/docs/concepts/chat_models/) | Built from configuration | Which model runs, and everything wrapped around it — tracing, rate limiting, a stub in tests |
| `checkpoints` | `daisy.Checkpoints` | `MemoryCheckpoints` | Where the conversation is saved, and therefore whether a session can resume |
| `jobs` | `daisy.JobStore` | `MemoryJobStore` | Where background jobs are recorded, and therefore whether one survives a restart |
| `observer` | `daisy.Observer` | None (dropped) | Where the audit trail goes — auto-approvals, goal changes, messages |
| `approvals` | `daisy.Approvals` | None (gates suspend) | Who answers a gated tool call when there is no human |
| `peers` | `SessionAccess` | None (composition tools absent) | How this session reaches other sessions |
| `sandbox` | `daisy.base.confinement.Profile` | Unconfined profile | What a tool's children may do |
| `configuration` | `GlobalConfiguration` | Read from XDG, **without creating it** | Providers, tuning, agent directories |

Two of these are interfaces we did not write. `BaseChatModel` is LangChain's, and the a2a
`TaskStore` behind the daemon's turn record is a2a's. Where the ecosystem already has an
interface, wrapping it would only add a second vocabulary for the same thing.

The rest are `typing.Protocol`s, which is the part that matters for you: they are *structural*.
Your object satisfies one by having the right methods. There is no base class to inherit, no
registry to join, and no import of Daisy in your type.

```python
class RedisCheckpoints:
    def __init__(self, client):
        self._client = client

    async def save(self, session_id, state):
        await self._client.set(f"daisy:{session_id}", json.dumps(state))

    async def load(self, session_id):
        raw = await self._client.get(f"daisy:{session_id}")
        return json.loads(raw) if raw else None

session = Session("general-assistant", checkpoints=RedisCheckpoints(redis))
```

That class inherits nothing and imports nothing of ours. It is accepted because it has `save`
and `load`; one that were missing `load` is rejected at the constructor, by name:

```
TypeError: checkpoints: RedisCheckpoints does not satisfy Checkpoints: it is missing `load`.
```

Structural typing gives no compile-time guarantee, so the check happens once per session
rather than surfacing as an `AttributeError` deep inside a turn.

### Approvals

By default a gated tool call does what it does under the daemon: the turn emits a `Suspended`
event and waits. That is right when a person is watching and wrong in a script, where the turn
hangs on a gate nobody will ever answer — which is why `ask()` raises rather than hanging.

An approver decides gates in code. Answering `None` means *no opinion*, and that gate suspends
as before, so you can auto-approve what you understand and still escalate the rest:

```python
from daisy import Approval, Session

class AllowReads:
    async def decide(self, gate):
        if gate.kind == "permission" and gate.risk in ("", "low"):
            return Approval(allow=True, reason="read-only work is pre-approved")
        return None          # anything riskier still asks a human

async with Session("general-assistant", approvals=AllowReads()) as session:
    print(await session.ask("summarise the test failures"))
```

An approver that raises escalates the gate rather than allowing it. A broken policy fails
closed.

### Observation

`Observer` receives what the harness decided but did not say out loud: a bash command
auto-approved and why, a goal updated, a message appended. Turn *events* are not this — those
come out of `stream()`.

```python
class LogObserver:
    def observe(self, observation):
        logger.info("%s %s", observation.kind, observation.data)

session = Session("general-assistant", observer=LogObserver())
```

`observe` may return an awaitable, which is scheduled rather than awaited — a synchronous
implementation that appends to a list is the common case, and one that writes to a database
should not have to block the turn. An observer that raises is logged and ignored: a turn must
not fail because its audit sink did.

## Driving a turn

`ask()` is the convenience. `stream()` is the whole vocabulary — text chunks, tool calls, tool
results, usage, suspensions, the same events a session sends a client over its socket:

```python
from daisy.runtime.turn_events import TextChunk, ToolCall, Suspended

async for event in session.stream("refactor the parser"):
    match event:
        case TextChunk(text=text):
            print(text, end="", flush=True)
        case ToolCall(tool_name=name):
            print(f"\n[{name}]")
        case Suspended(interactions=gates):
            ...  # answer them, or stop
```

The conversation is checkpointed when a turn ends, including when it ends badly — a turn that
raised has still changed the conversation, and losing that is worse than recording a failure.

Resuming is giving a new `Session` the same id and the same store:

```python
store = MemoryCheckpoints()
async with Session("general-assistant", session_id="review", checkpoints=store) as first:
    await first.ask("read src/parser.py")

async with Session("general-assistant", session_id="review", checkpoints=store) as second:
    await second.ask("now what would you change?")   # remembers the file
```

`session.runtime` is the `AgentRuntime` underneath, deliberately public. A library that hides
its own core forces every non-obvious use into a fork.

## When to use the daemon instead

Reach for `daisyd` when you want a session that outlives the terminal that started it, a
harness reachable from another machine, crash isolation between sessions, or peer composition.
Those are what a control plane is *for*, and none of them can be had from an object in your
process.

The two are the same harness. A daemon session is this same runtime, in a process forked from
the prototype, with a socket in front of it.
