# The desktop app

A native macOS window over the same control plane the `frank` command uses. It holds no harness
of its own: it finds a daemon, talks to it, and shows you what it says. Anything the app can do,
the command can do, and the reverse.

Start it with the daemon in one step:

```shell
frank app
```

That brings the daemon up if it is not running, then opens the window. `frank app --no-daemon`
opens only the window, for when a daemon is already up or lives on another machine.

## What the window shows

| Region | What it is |
|---|---|
| The sidebar | Your projects, and the sessions in each. A session that created others nests under it |
| The transcript | The conversation, as it happens: prose, tool calls, tool results, and prompts that need an answer |
| The composer | Where you type. It also queues a message for the next turn while one is running |
| Settings | Providers and keys, agents, environments, permissions, and the screen tools |

A dot beside a session says what it is doing. A pulsing grey dot means it is working. A yellow
dot means it is parked on a decision only you can make. A blue dot means it finished something
while you were looking elsewhere. A session with no dot is idle, or asleep. Those are the same thing to you: the next message wakes it in about 60 ms.

## Answering a decision

When a session needs permission, the turn stops and the prompt appears above the composer. It
says what the tool wants to do and why it is being asked. Allow it or deny it, and the turn goes
on. There is no "always allow": every decision is allow-once or deny, and a session's permission
mode can be changed at any point from the chip under the composer, including mid-turn. See [Configuration](configuration.md#permission-modes).

You can leave a session parked for as long as you like. The whole turn is checkpointed on disk,
and the session sleeps rather than holding a process open to wait for you.

## Environments

A project is a set of **environments**. An environment says where a session's work happens: a directory on this machine, or one on an SSH host. Add one in **Settings → Environments**. The SSH hosts
come from your `~/.ssh/config`, so a host you already use is one you can pick — and picking one fills the path in with that host's home directory, since you cannot be expected to know its layout.

An environment also carries the permission mode that its sessions start with. A scratch directory and a production checkout can therefore behave differently, with nothing for you to remember. Adding or editing one reaches the sessions already running in that workspace: they pick it up on their next turn rather than only after a restart.

## Screen control

The app can drive native macOS applications and your own Chrome, through the agent's
`control_screen` tool. It is off until you turn it on, and it needs two grants:

- **Accessibility**, which macOS asks for once. It is tied to the app's code identity, so a
  signed build keeps the grant across updates.
- **Chrome's remote-debugging port**, which the app tells you how to enable.

The tool reads the accessibility tree and the page's structure, not screenshots. See
[Tools](tools.md#screen-control-control_screen) for what it can do and what it cannot.

## A daemon somewhere else

The app is a client, so the daemon it talks to does not have to be on this machine. An environment on
an SSH host runs its tools there while the daemon stays here. To put the *daemon* on another
machine, forward its port and point the app at it — see
[Architecture](architecture.md#connections-local-remote-ssh).

## When there is no daemon

The app does not start a harness of its own. With nothing listening it says so and tells you
what to run, exactly as it would if a remote host were not answering.

## Where to go next

- Every setting the app exposes: [Configuration](configuration.md).
- The same operations from a terminal: [The `frank` command](cli.md).
- Writing your own agents and skills: [Agents and skills](agents-and-skills.md).
