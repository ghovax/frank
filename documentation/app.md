# The desktop app

A native macOS window over the same control plane the `langmesh` command uses. It holds no harness of its own: it finds a daemon, talks to it, and shows you what it says. Anything the app can do, the command can do, and the reverse.

Start it with the daemon in one step:

```shell
langmesh app
```

That brings the daemon up if it is not running, then opens the window. `langmesh app --no-daemon` opens only the window, for when a daemon is already up or lives on another machine.

## What the window shows

| Region | What it is |
|---|---|
| The sidebar | Your projects, and the conversations you started in each |
| The transcript | The conversation, as it happens: prose, tool calls, tool results, and prompts that need an answer |
| The composer | Where you type. It also queues a message when the session cannot take one yet |
| Delegated work | A panel on the right, opened from the top bar: the conversation you are in and everything it handed off, as a tree |
| Settings | Providers and keys, agents, environments, permissions, and the screen tools |

The sidebar lists conversations you started, and only those. A session that a session created is delegated work rather than a conversation of yours, so it lives in its own panel — where the tree is the point — instead of nesting inside a list you navigate by.

A dot beside a session says what it is doing. A pulsing grey dot means it is working. A yellow dot means it is parked on a decision only you can make. A blue dot means it finished something while you were looking elsewhere. A session with no dot is idle, or asleep. Those are the same thing to you: the next message wakes it in about 60 ms.

## Answering a decision

When a session needs permission, the turn stops and the prompt appears above the composer. It says what the tool wants to do and why it is being asked. Allow it or deny it, and the turn goes on. There is no "always allow": every decision is allow-once or deny, and a session's permission mode can be changed at any point from the chip under the composer, including mid-turn. See [Configuration](configuration.md#permission-modes).

You can keep typing while the prompt is up. A parked session takes no message — accepting one would mean discarding the parked turn — so what you type is **queued**, marked as waiting on your decision, and sent the moment you make it. It goes into the turn that resumes, so a correction you thought of while reading the prompt reaches the work it is about, rather than waiting for the whole turn to end.

You can leave a session parked for as long as you like. The whole turn is checkpointed on disk, and the session sleeps rather than holding a process open to wait for you.

## Environments

A project is a set of **environments**. An environment says where a session's work happens: a directory on this machine, or one on an SSH host. Add one in **Settings**, under **Environments**. The SSH hosts come from your `~/.ssh/config`, so a host you already use is one you can pick — and picking one fills the path in with that host's home directory, since you cannot be expected to know its layout.

An environment also carries the permission mode that its sessions start with. A scratch directory and a production checkout can therefore behave differently, with nothing for you to remember. Adding or editing one reaches the sessions already running in that workspace: they pick it up on their next turn rather than only after a restart.

## Screen control

The app can drive native macOS applications and your own Chrome, through the agent's `control_screen` tool. It is off until you turn it on, and it needs two grants:

- **Accessibility**, which macOS asks for once. It is tied to the app's code identity, so a signed build keeps the grant across updates.
- **Chrome's remote-debugging port**, which the app tells you how to enable.

The tool reads the accessibility tree and the page's structure, not screenshots. See [Tools](tools.md#screen-control-control_screen) for what it can do and what it cannot.

## A daemon somewhere else

The app is a client, so the daemon it talks to does not have to be on this machine. An environment on an SSH host runs its tools there while the daemon stays here. To put the *daemon* on another machine, forward its port and point the app at it — see [Architecture](architecture.md#connections-local-remote-ssh).

## When there is no daemon

The app does not start a harness of its own. With nothing listening it says so and tells you what to run, exactly as it would if a remote host were not answering.

## Where to go next

- Every setting the app exposes: [Configuration](configuration.md).
- The same operations from a terminal: [The `langmesh` command](cli.md).
- Writing your own agents and skills: [Agents and skills](agents-and-skills.md).
