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
| `catalogue` | `daisy.Catalogue` | The working directory's `.agents` plus the packaged base layer — **and nothing of `$HOME`** | Where agents, skills, memories, instructions and prompt templates come from |
| `providers` | `{"anthropic": "sk-..."}` or `{"custom": {"api_key": ..., "base_url": ...}}` | Whatever the machine is configured with | Provider credentials, in code |
| `model_identifier` | `"provider/model"` | The agent profile's own | Which model this session runs, overriding the profile |
| `configuration` | `GlobalConfiguration` | Read from XDG, **without creating it** | Providers, tuning, agent directories |
| `agent` | `str` name **or** an `AgentConfiguration` you build | — (required) | The agent itself: prompt, model, permission mode, which built-in tools it has |
| `tools` | LangChain [`BaseTool`](https://python.langchain.com/docs/concepts/tools/) | None | Tools the agent gains, on top of the harness's |
| `permissions` | A `PermissionEvaluator`-shaped object | The built-in rule engine | Whether a call is gated at all |
| `tool_risk` | `"none"`/`"low"`/`"medium"`/`"high"` | `"medium"` | What a supplied tool is gated at |
| `transcript` | `daisy.Transcript` | `MemoryTranscript` | Where the record of completed turns goes |
| `credentials` | `daisy.Credentials` | A `0600` file under XDG | Where account tokens live (bypassed entirely by `model=`) |
| `locations` | `LocationExecutor` records | Local, at `directory` | Where tools may run — SSH, containers |
| `workspace` | `SessionWorkspaceManager` | None — **opt in via `prepare_workspace()`** | A git worktree per session |
| `tracer_provider` | OpenTelemetry `TracerProvider` | The process-wide one, if configured | Where spans go, per session |

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

### Your own tools

The one thing configuration cannot do is *extend*. `tools=` takes LangChain `BaseTool`s — adopted, not wrapped, so anything already written for that ecosystem works unchanged:

```python
from langchain_core.tools import tool
from daisy import Session

@tool
def house_price(address: str) -> str:
    """Look up what a house last sold for."""
    return db.lookup(address)

async with Session("general-assistant", tools=[house_price]) as session:
    print(await session.ask("what did 12 Elm St sell for?"))
```

A supplied tool goes through the *same* preamble every built-in does — permission resolved, location resolved, policy applied — because the extension point is the handler, not the pipeline. Two consequences worth knowing:

- **It is gated at `tool_risk`, which defaults to `"medium"`.** The permission engine classifies by tool name and has never heard of yours, so there is no honest way to infer what it does; defaulting to *ask* means adding a tool cannot silently widen what a session may do. `tool_risk="none"` says otherwise deliberately.
- **It cannot shadow a built-in.** A tool named `bash` that is not this harness's `bash` would be a confinement surprise, not an extension point, so a name collision resolves to ours.
- **The agent profile's `tools_enabled` list does not filter it.** That list narrows the *harness's* capabilities and was written before your program existed; a supplied tool would otherwise vanish for every agent that names an explicit list.

### Building the agent itself

`agent=` takes a name to load from disk *or* an `AgentConfiguration` you construct. With a
constructed one, nothing on the machine is consulted — the agent is a value your program owns:

```python
from daisy import Session
from daisy.base.configuration import AgentConfiguration, BashToolConfiguration, ToolsConfiguration

reviewer = AgentConfiguration(
    name="reviewer",
    provider="anthropic",
    model="claude-sonnet-4",
    system_prompt="You review code. Be terse.",
    permission_mode="read_only",
    tools_enabled=["read_file", "search_code"],       # an allow-list
    tools=ToolsConfiguration(
        disabled=["fetch_url"],                        # …or a deny-list
        bash=BashToolConfiguration(enabled=False, background_allowed=False),
    ),
)

async with Session(reviewer, directory=".") as session:
    print(await session.ask("what changed and is it safe?"))
```

Under-specify it and the error says what to do rather than failing obscurely:

```
ValueError: Agent 'reviewer' names no model. Set `provider` and `model` in its profile, pass
`model_identifier="provider/model"` to `daisy.Session`, or hand the runtime a `model=` of your own.
```

**Narrowing the built-in tools** has two complementary forms. `tools_enabled` is an allow-list,
so naming one tool means naming all of them — right for an agent defined by a small capability
set. `tools.disabled` is a deny-list — right when an agent should have everything *except*
shell access. Both are enforced twice: the roster decides what the model is offered, and the
gate decides what it may run, because a model can call a tool it was never offered.

`permissions=` replaces the rule engine outright, for a program whose policy is its own.
`Approvals` answers a gate once the engine has decided there should be one; `permissions=`
decides whether there is one at all.

### The transcript

`Checkpoints` answers "resume this conversation". `Transcript` answers "what has this session done" — one entry per completed turn, with what was asked, what came back, how it ended and what it cost:

```python
async with Session("general-assistant", session_id="nightly") as session:
    await session.ask("audit the dependency tree")

for turn in await session.transcript.turns("nightly"):
    print(turn.outcome, turn.tools_called, turn.input_tokens + turn.output_tokens)
```

Deliberately **not** a2a's `TaskStore`. The daemon speaks A2A and its record is rightly one; the library speaks no A2A, and handing it Tasks would add a protocol it does not use to solve a problem it does not have.

### Credentials and the model

A library whose only way to be given an API key is a YAML file in the user's home directory is
not a library. Pass them in:

```python
session = Session(
    "general-assistant",
    providers={"anthropic": os.environ["MY_APP_ANTHROPIC_KEY"]},
    model_identifier="anthropic/claude-sonnet-4",
)
```

`providers` is merged onto whatever configuration is in play rather than replacing it, so a
program can supply one key and inherit the rest — and the providers' conventional environment
variables keep the precedence they already had, so a deployment that injects them keeps
working. The long form takes a `base_url` too, for an OpenAI-compatible endpoint.

`model_identifier` overrides the agent profile's own choice, because the common case for an
embedder is one agent definition run against whichever model *their* program is configured for,
and editing a profile file to express a runtime choice is the wrong shape.

If you already hold a configured `BaseChatModel`, `model=` skips all of this — no credential of
ours is consulted, because none is needed.

### The catalogue

Everything the prompt is assembled from — the agent profile, the skills, the memories, the project's instructions, the prompt templates themselves — comes from one interface, because those differ in how they are *parsed*, not in how they are *found*.

The default matters more here than anywhere else. Before this seam existed, finding that material meant walking hardcoded paths, and the instruction loader read `~/.config/opencode/AGENTS.md` and `~/.claude/CLAUDE.md` — two *other products'* configuration files — out of the user's home directory unconditionally. So a library session's default catalogue reads the working directory and the packaged agents, and nothing of `$HOME`. `daisyd` and the CLI use `machine_catalogue`, which does read all of it, because there the person running it is the person those files describe.

Build one entirely in code when you want the prompt fully under your control:

```python
from daisy.base.catalogue import DictCatalogue
from daisy.base.configuration import AgentConfiguration
from daisy.base.skills import Skill

catalogue = DictCatalogue(
    agent_configurations={"reviewer": AgentConfiguration(identifier="reviewer", ...)},
    skill_list=[Skill(name="house-style", description="our conventions", body=STYLE)],
    instruction_text='[{"path": "in-memory", "content": "always cite line numbers"}]',
)
session = Session("reviewer", catalogue=catalogue)
```

Unlisted prompt templates fall back to the packaged ones, so replacing the system prompt is something you opt into rather than something you have to reproduce to get started. And `agent=` accepts an `AgentConfiguration` directly as well as a name, which is the shortest path of all when you have one in hand.

`FileCatalogue` is the other shipped implementation — it is what the harness has always done, with the roots as an argument instead of derived.

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

### Telemetry and workspaces

`tracer_provider=` binds a tracer for this session rather than reconfiguring the process, so two sessions in one program can report to different places. `credentials=` is bound the same way. Both unbind when the session closes.

A git worktree per session is opt-in, because it writes to disk and every other default here is chosen so a library session leaves nothing behind:

```python
session = Session("general-assistant", directory="/path/to/repo")
runtime_directory = await session.prepare_workspace()   # its own worktree
await session.ask("refactor the parser and run the tests")
```

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
