# Frank as a library

**The library is the bottom of the stack, and everything else is built on it.**

| Layer | What it is | What it knows about your machine |
|---|---|---|
| `frank.Session` | The harness: the turn loop, the tools, the prompts, the permissions | Nothing. Every value is one you passed |
| `frank.daemon.machine` | The loaders that turn a home directory into what `Session` takes | The XDG paths, and your `.agents` |
| `frankd` | Supervision: a process per session, a socket each, the databases | Everything, and it is the right place to |
| `frank`, and the app | Clients of the daemon | Where the daemon is |

`Session` runs an agent in your own process. It reads no path you did not give it, resolves no
name against anything, and leaves nothing behind. That is what makes it embeddable. A library that writes a database into your home directory, because you imported it, is one you cannot ship inside something else.

Everything a session needs can be built in code:

```python
import asyncio
from frank import AgentConfiguration, DictCatalogue, Session

reviewer = AgentConfiguration(
    name="reviewer",
    description="Reads a change and reports what it would break.",
    system_prompt="You review changes. Name the risk, or say there is none.",
    permission_mode="read_only",
    provider="anthropic",
    model="claude-opus-4-5",
)

async def main() -> None:
    async with Session(
        reviewer,
        directory="/srv/checkout",
        catalogue=DictCatalogue(agent_configurations={"reviewer": reviewer}),
        providers={"anthropic": "sk-ant-…"},
    ) as session:
        print(await session.ask("What would break if I removed the retry loop in the fetcher?"))

asyncio.run(main())
```

No configuration file, no `.agents` directory, no `$HOME`. The agent, its prompt, its
permission mode and its credentials are all values in the program.

Use it for three things:

- Embed the harness in another program.
- Write a terminal interface that shares code with the browser one, instead of reimplementing it.
- Run a one-shot agent in a script.

## Taking what the machine has

A program that *is* running on someone's machine — a CLI, a scheduled job — can ask for the
machine's agents deliberately. The import says what it is doing:

```python
from frank import Session
from frank.daemon.machine import load_agent, load_catalogue, load_configuration

configuration = load_configuration(seed=False)
directory = "/Users/you/code/project"

async with Session(
    load_agent("general-assistant", directory, configuration=configuration),
    directory=directory,
    configuration=configuration,
    catalogue=load_catalogue(configuration, directory),
) as session:
    print(await session.ask("What does this project do?"))
```

Four lines that touch the machine, each one written by you. `frank.daemon.machine` is the only
module that knows XDG exists, and `frank.Session` never imports it.

## What you give up

A library session is an object, not a process. It has none of the three properties `frankd`
exists to provide:

| Property | Library | Daemon |
|---|---|---|
| Addressable from outside | No | Yes — a socket, a token, `frank send` |
| Outlives the program that made it | No | Yes |
| Crash-isolated | No — a tool that exhausts memory takes you with it | Yes — one process per session |
| Peers (`create_session` and friends) | Only if you supply `peers` | Yes |
| Confinement of tool children | **Identical** | **Identical** |

Confinement surprises people, so here it is plainly. A session process was never sandboxed; its *tool children* are, and a child is confined at the moment it is spawned. That is the same code on both paths.

## The seams

Everything durable is a constructor argument with an interface behind it. The defaults suit a program that is not a daemon. For anything durable, that means *in memory*. A library that writes a database into your home directory, because you ran one background command, is a library you cannot embed.

| Argument | Interface | Default | What it decides |
|---|---|---|---|
| `model` | LangChain [`BaseChatModel`](https://python.langchain.com/docs/concepts/chat_models/) | Built from configuration | Which model runs, and everything wrapped around it — tracing, rate limiting, a stub in tests |
| `checkpoints` | `frank.Checkpoints` | `MemoryCheckpoints` | Where the conversation is saved, and therefore whether a session can resume |
| `jobs` | `frank.JobStore` | `MemoryJobStore` | Where background jobs are recorded, and therefore whether one survives a restart |
| `observer` | `frank.Observer` | None (dropped) | Where the audit trail goes — auto-approvals, goal changes, messages |
| `approvals` | `frank.Approvals` | None (gates suspend) | Who answers a gated tool call when there is no human |
| `peers` | `SessionAccess` | None (composition tools absent) | How this session reaches other sessions |
| `sandbox` | `frank.base.confinement.Profile` | Unconfined profile | What a tool's children may do |
| `catalogue` | `frank.Catalogue` | The working directory's `.agents` plus the packaged base layer — **and nothing of `$HOME`** | Where agents, skills, memories, instructions and prompt templates come from |
| `providers` | `{"anthropic": "sk-..."}` or `{"custom": {"api_key": ..., "base_url": ...}}` | Whatever the machine is configured with | Provider credentials, in code |
| `model_identifier` | `"provider/model"` | The agent profile's own | Which model this session runs, overriding the profile |
| `configuration` | `GlobalConfiguration` | Read from XDG, **without creating it** | Providers, tuning, agent directories |
| `agent` | `str` name **or** an `AgentConfiguration` you build | — (required) | The agent itself: prompt, model, permission mode, which built-in tools it has |
| `tools` | LangChain [`BaseTool`](https://python.langchain.com/docs/concepts/tools/) | None | Tools the agent gains, on top of the harness's |
| `permissions` | A `PermissionEvaluator`-shaped object | The built-in rule engine | Whether a call is gated at all |
| `tool_risk` | `"none"`/`"low"`/`"medium"`/`"high"` | `"medium"` | What a supplied tool is gated at |
| `transcript` | `frank.Transcript` | `MemoryTranscript` | Where the record of completed turns goes |
| `credentials` | `frank.Credentials` | A `0600` file under XDG | Where account tokens live (bypassed entirely by `model=`) |
| `locations` | `LocationExecutor` records | Local, at `directory` | Where tools may run — SSH, containers |
| `workspace` | `SessionWorkspaceManager` | None — **opt in via `prepare_workspace()`** | A git worktree per session |
| `tracer_provider` | OpenTelemetry `TracerProvider` | The process-wide one, if configured | Where spans go, per session |

Two of these are interfaces we did not write. `BaseChatModel` is LangChain's, and the a2a
`TaskStore` behind the daemon's turn record is a2a's. Where the ecosystem already has an
interface, wrapping it would only add a second vocabulary for the same thing.

The rest are `typing.Protocol`s, which is the part that matters for you: they are *structural*.
Your object satisfies one by having the right methods. There is no base class to inherit, no
registry to join, and no import of Frank in your type.

```python
class RedisCheckpoints:
    def __init__(self, client):
        self._client = client

    async def save(self, session_id, state):
        await self._client.set(f"frank:{session_id}", json.dumps(state))

    async def load(self, session_id):
        raw = await self._client.get(f"frank:{session_id}")
        return json.loads(raw) if raw else None

session = Session(reviewer, directory="/srv/checkout", checkpoints=RedisCheckpoints(redis))
```

That class inherits nothing and imports nothing of ours. The harness accepts it because it has `save` and `load`. One that lacks `load` fails at the constructor, by name:

```text
TypeError: checkpoints: RedisCheckpoints does not satisfy Checkpoints: it is missing `load`.
```

Structural typing gives no compile-time guarantee. The check happens once per session
rather than failing part-way through a turn, far from the call that supplied it.

### Your own tools

The one thing configuration cannot do is *extend*. `tools=` takes LangChain `BaseTool`s — adopted, not wrapped, so anything already written for that ecosystem works unchanged:

```python
from langchain_core.tools import tool
from frank import Session

@tool
def open_incidents(service: str) -> str:
    """Every open incident for a service, newest first."""
    return incidents.query(service=service, status="open")

async with Session(reviewer, directory="/srv/checkout", tools=[open_incidents]) as session:
    print(await session.ask("Are there open incidents on the checkout service?"))
```

A supplied tool goes through the *same* preamble as every built-in: permission resolved, location resolved, policy applied. The extension point is the handler, not the pipeline. Two consequences follow:

- **It is gated at `tool_risk`, which defaults to `"medium"`.** The permission engine classifies by tool name, and it does not know yours. There is no honest way to infer what your tool does. The default is *ask*, so a new tool cannot silently widen what a session may do. Set `tool_risk="none"` to say otherwise deliberately.
- **It cannot shadow a built-in.** A tool named `bash` that is not this harness's `bash` is a confinement surprise, not an extension point. A name collision therefore resolves to ours.
- **The agent profile's `tools_enabled` list does not filter it.** That list narrows the *harness's* capabilities, and someone wrote it before your program existed. Otherwise a supplied tool disappears for every agent that names an explicit list.

### Building the agent itself

`agent=` takes a name to load from disk *or* an `AgentConfiguration` you construct. With a
constructed one, nothing on the machine is consulted — the agent is a value your program owns:

```python
from frank import Session
from frank.base.configuration import AgentConfiguration, BashToolConfiguration, ToolsConfiguration

reviewer = AgentConfiguration(
    name="reviewer",
    provider="anthropic",
    model="claude-sonnet-4",
    system_prompt="You review code. Be terse.",
    permission_mode="read_only",
    tools_enabled=["read_file", "search_code"],
    tools=ToolsConfiguration(
        disabled=["fetch_url"],
        bash=BashToolConfiguration(enabled=False, background_allowed=False),
    ),
)

async with Session(reviewer, directory="/srv/checkout") as session:
    print(await session.ask("What changed on this branch, and is it safe to ship?"))
```

Under-specify it and the error says what to do rather than failing obscurely:

```text
ValueError: Agent 'reviewer' names no model. Set `provider` and `model` in its profile, pass
`model_identifier="provider/model"` to `frank.Session`, or hand the runtime a `model=` of your own.
```

**Narrowing the built-in tools** has two complementary forms. `tools_enabled` is an allow-list, so naming one tool means naming all of them — right for an agent defined by a small capability set. `tools.disabled` is a deny-list — right when an agent should have everything *except* shell access. Both are enforced twice.

The roster decides what the model is offered. The gate decides what it may run. A model can call a tool it was never offered.

`permissions=` replaces the rule engine outright, for a program whose policy is its own.
`Approvals` answers a gate once the engine has decided there should be one; `permissions=`
decides whether there is one at all.

### The transcript

`Checkpoints` answers "resume this conversation". `Transcript` answers "what has this session done", with one entry per completed turn. Each entry records what was asked, what came back, how it ended, and what it cost:

```python
nightly = "session-8f9c724a-ce51-41b3-83a9-f5969b22a9e2"

async with Session(reviewer, directory="/srv/checkout", session_id=nightly) as session:
    await session.ask("Audit the dependency tree and flag anything unmaintained.")

for turn in await session.transcript.turns(nightly):
    print(turn.outcome, turn.tools_called, turn.input_tokens + turn.output_tokens)
```

Deliberately **not** a2a's `TaskStore`. The daemon speaks A2A, and its record is rightly an A2A one. The library speaks no A2A. To hand it Tasks would add a protocol it does not use, for a problem it does not have.

### Credentials and the model

A library whose only way to be given an API key is a YAML file in the user's home directory is
not a library. Pass them in:

```python
session = Session(
        reviewer,
    directory="/srv/checkout",
    providers={"anthropic": os.environ["MY_APP_ANTHROPIC_KEY"]},
    model_identifier="anthropic/claude-opus-4-5",
)
```

`providers` merges onto the configuration in play; it does not replace it. A program can therefore supply one key and inherit the rest. The providers' conventional environment variables keep the precedence they had, so a deployment that injects them continues to work. The long form takes a `base_url` too, for an OpenAI-compatible endpoint.

`model_identifier` overrides the agent profile's own choice. The common case for an embedder is one agent definition, run against whichever model *their* program is configured for. To edit a profile file for a runtime choice is the wrong shape.

If you already hold a configured `BaseChatModel`, `model=` skips all of this — no credential of
ours is consulted, because none is needed.

### The catalogue

One interface supplies everything the prompt is assembled from: the agent profile, the skills, the memories, the project's instructions, and the prompt templates. These differ in how the harness *parses* them, not in how it *finds* them.

The default matters more here than anywhere else. A library must not read another product's configuration out of your home directory, and must not walk hardcoded paths to find prompt material.

A library session's default catalogue therefore reads the working directory and the packaged agents, and nothing of `$HOME`. `frankd` and the CLI use `machine_catalogue`, which does read all of it, because there the person running it is the person those files describe.

Build one entirely in code when you want the prompt fully under your control:

```python
from frank.base.catalogue import DictCatalogue
from frank.base.configuration import AgentConfiguration
from frank.base.skills import Skill

catalogue = DictCatalogue(
    agent_configurations={"reviewer": reviewer},
    skill_list=[
        Skill(
            name="house-style",
            description="How this codebase names things and orders imports.",
            body=HOUSE_STYLE,
        ),
    ],
    instruction_text='[{"path": "in-memory", "content": "Always cite file and line."}]',
)
session = Session(reviewer, directory="/srv/checkout", catalogue=catalogue)
```

Unlisted prompt templates fall back to the packaged ones. You therefore opt in to replace the system prompt. You do not have to reproduce it to get started. And `agent=` accepts an `AgentConfiguration` directly as well as a name, which is the shortest path of all when you have one in hand.

`FileCatalogue` is the other shipped implementation — it is what the harness has always done, with the roots as an argument instead of derived.

### Approvals

By default a gated tool call does what it does under the daemon: the turn emits a `Suspended`
event and waits. That is right when a person watches, and wrong in a script. In a script the turn stops at a gate that nobody will answer. `ask()` therefore raises instead.

An approver decides gates in code. Answer `None` to give *no opinion*; that gate then suspends as before. You can therefore auto-approve what you understand, and still escalate the rest:

```python
from frank import Approval, Session

class AllowReads:
    async def decide(self, gate):
        if gate.kind == "permission" and gate.risk in ("", "low"):
            return Approval(allow=True, reason="Reads are pre-approved for this job.")
        return None

async with Session(reviewer, directory="/srv/checkout", approvals=AllowReads()) as session:
    print(await session.ask("Summarise the test failures on the current branch."))
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

session = Session(reviewer, directory="/srv/checkout", observer=LogObserver())
```

`observe` can return an awaitable. The harness schedules it; it does not await it. A synchronous implementation that appends to a list is the common case. One that writes to a database must not block the turn. An observer that raises is logged and ignored: a turn must
not fail because its audit sink did.

### Telemetry and workspaces

`tracer_provider=` binds a tracer for this session rather than reconfiguring the process, so two sessions in one program can report to different places. `credentials=` is bound the same way. Both unbind when the session closes.

A git worktree per session is opt-in, because it writes to disk. Every other default here leaves nothing behind:

```python
session = Session(reviewer, directory="/srv/checkout")
runtime_directory = await session.prepare_workspace()
await session.ask("Refactor the parser to use the streaming reader, then run the tests.")
```

## Driving a turn

`ask()` is the convenience. `stream()` is the whole vocabulary — text chunks, tool calls, tool
results, usage, suspensions, the same events a session sends a client over its socket:

```python
from frank.runtime.turn_events import TextChunk, ToolCall, Suspended

async for event in session.stream("Refactor the parser to use the streaming reader."):
    match event:
        case TextChunk(text=text):
            print(text, end="", flush=True)
        case ToolCall(tool_name=name):
            print(f"\n[{name}]")
        case Suspended(interactions=gates):
            ...
```

The harness checkpoints the conversation when a turn ends, including when it ends badly. A turn that raised still changed the conversation. To lose that is worse than to record a failure.

Resuming is giving a new `Session` the same id and the same store:

```python
store = MemoryCheckpoints()
review = "session-3d965dfe-21c4-4f2c-9040-290e77bea0b1"

async with Session(reviewer, directory="/srv/checkout", session_id=review, checkpoints=store) as first:
    await first.ask("Read src/parser.py and tell me what it assumes about its input.")

async with Session(reviewer, directory="/srv/checkout", session_id=review, checkpoints=store) as second:
    await second.ask("Now what would you change?")
```

`session.runtime` is the `AgentRuntime` underneath, deliberately public. A library that hides
its own core forces every non-obvious use into a fork.

## When to use the daemon instead

Reach for `frankd` when you want one of these:

- A session that outlives the terminal that started it.
- A harness you can reach from another machine.
- Crash isolation between sessions.
- Peer composition.
Those are what a control plane is *for*, and none of them can be had from an object in your
process.

The two are the same harness. A daemon session is this same runtime, in a process forked from
the prototype, with a socket in front of it.
