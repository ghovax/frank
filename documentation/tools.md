# Tools

A session acts through tools, and every tool call runs inside the session's [confinement](configuration.md#the-sandbox). A call that stays inside it runs without asking anybody. A call that asks to reach past it pauses under `ask` and reaches you as a prompt in the app, or as `frank allow` / `frank deny` in the terminal; under `automatic` it never pauses, and the reviewer allows or refuses it. The description the model reads is in the repo: a docstring in `src/frank/runtime/tools/registry.py` for most tools, a template in `src/frank/runtime/prompts/tool_*.md` for the peer-session ones.

There is no delegation tool and no in-process sub-agent. A session that needs a peer creates one with `create_session`, which reaches the same control plane your terminal does. See [Composing with other sessions](#composing-with-other-sessions).

## The built-in surface

**Shell and files**

| Tool | What it does |
|------|--------------|
| `bash` | Run shell commands. Sandboxed to the workspace by default; per-command rules per agent. |
| `read_file` | Read a file (with line ranges and image support). |
| `write_file` | Create or overwrite a file. |
| `edit_file` | Make a targeted edit to an existing file. |
| `search_code` | Semantic code search over the repository. |
| `download_file` | Download a file from a URL to disk. |

There are no dedicated `find_files`/`search_content` tools; for literal file-name and content search, use `bash` with ripgrep (`rg`) and `fd`.

A session can also install what it does not have. Where the machine has Nix, each session gets a package profile of its own on `PATH` and installs into it with an ordinary `nix profile add nixpkgs#<name>` — no path, no flag. What it installs belongs to that session and is deleted with it, and the confinement is untouched. See [Configuration](configuration.md#the-session-toolbox).

**Web**

| Tool | What it does |
|------|--------------|
| `search_web` | Search the web (Exa-backed fallback). |
| `fetch_url` | Fetch and read a page via a tiered engine: Jina, then Firecrawl, then direct. |

**Orchestration and knowledge**

| Tool | What it does |
|------|--------------|
| `set_tasks` / `update_tasks` | Maintain a task list for a multi-step job. |
| `update_goal` | Set the outcome the session is working toward, and say when it is met, blocked, or no longer wanted. |
| `read_turn` | Read a sibling turn handed to this session from outside. |
| `load_skill` | Load a `SKILL.md` capability on demand. |
| `ask_user` | Ask the user a question and wait for the answer. |
| `wait_for` | Pause for a few seconds without a model round trip, to re-check something that was not ready. |

A goal is not a longer task list. The task list is the steps; the goal is the outcome the steps are for, and the difference is what happens when a turn ends. A session with an open goal keeps going: it opens itself another turn, with the goal restated, until the agent satisfies it, clears it, or reports it blocked — bounded by an allowance for how long it runs with nobody watching, after which the goal is parked and waits for you. Setting one takes both the end state and the conditions that would prove it, and satisfying one takes the evidence that each was met; a goal that cannot be audited is one that gets called done from memory. See [Architecture](architecture.md#goals).

**Peer sessions**

## Composing with other sessions


| Tool | What it does |
|------|--------------|
| `create_session` | Create an idle peer session. Its `agent` argument enumerates the profiles actually installed, so an unknown name cannot be asked for. Returns as soon as the peer exists. |
| `message_session` | Message another session — one you created, or the one that created you. A session already mid-turn has the message *steered* into that turn at its next safe point; an idle one starts a new turn. A session **parked on a human decision** takes neither: the call reports that nothing was sent and says what the session is waiting on. |
| `read_session` | One session's state: its profile, whether its process is alive, whether a turn is in flight, whether it is waiting on a human. |
| `list_sessions` | The sessions this one created. Its own subtree, not the machine's. |

The caller is the parent, always — it is not an argument. That puts a peer inside the tree, inside the reaper, and under the permission clamp. A peer can therefore never hold authority that its parent does not have.

The peer starts with a copy of its parent's model-facing conversation, while keeping its own agent profile, permissions, process, and context window. The unfinished `create_session` tool call is excluded so its history begins at a valid message boundary.

**A peer answers by messaging.** When the peer finishes, it calls `message_session` on the session that created it. That session's id is in its context, as `parent_session`. The message lands in the caller's context like any other inbound message. `create_session` therefore does not wait, and there is no handle to hold.

Nothing reconstructs a result. The peer decides its own answer, in its own words, at the moment it knows. A caller starts the work, carries on with whatever does not depend on it, and ends its turn — the reply wakes it.

That message arrives as a **peer turn**, not a user turn. The wire carries the distinction under the harness's one extension key, `urn:frank:ext:turn:v1`. It sends `kind` plus `peerSender` to name the sender.

Without that, a peer's report reaches the model as an instruction from the person it works for. It also appears in the transcript as words the user never wrote.

A peer that dies before it reports cannot say so. That is the one thing the harness says for it. The daemon tells the parent when it reaps a child, with the child's id and the reason it ended.

**Agents on other hosts**

`list_remote_agents` and `message_remote_agent` are separate verbs, because a remote agent is a separate bargain. It runs on someone else's machine, at someone else's cost. It has no shared history and no access to this filesystem. Present only when one is registered.

**MCP**

`call_mcp_tool`, `list_mcp_tools`, `list_mcp_resources`, `read_mcp_resource` — bridge to any configured [MCP server](agents-and-skills.md#mcp-servers).

## Screen control (`control_screen`)

Frank drives the live screen through one tool, `control_screen`, covering native macOS apps and **your own Chrome**. Its Python script both finds elements and acts on them. It is **macOS-only** and **opt-in**: gated by `computer_control.enabled` (off by default; see [Configuration guide](configuration.md#execution-and-permissions)).

**Finding — read the live surface.** Inside the script, `find_many(query)` and `find_one(query)` take a plain-language query. They return the matching UI as **ranked elements** to act on, not as pixels. Each element carries a stable `id`, its role, its text, and its context. On native apps this reads the **accessibility tree**. On Chrome it reads the page's real semantic structure, roles and names, iframes included. It uses the Chrome DevTools Protocol through Playwright.

It also surfaces the page's own **network and API requests**. The agent can therefore find the endpoints that the page calls. `find_one` returns the single best match and raises if the top matches are indistinguishable, so an unclear target is caught rather than guessed.

**Acting — a composed script of trusted-input primitives.** The same script drives the elements that a find returned. It addresses them by `id`, or by a query resolved the same way. It uses **trusted input**: click, type, scroll, `evaluate`, and the like. The script is ordinary Python, so a whole task is a single call. It can loop over rows, branch on what it finds, and call the page's own API in one line.

It does not need a round trip for each action. On the browser, `evaluate` can **replay the page's own authenticated API in-page**, reusing the logged-in session instead of re-authenticating. Actions run against the real surface. Browser clicks go through Playwright's actionability checks.

**Targets — the place a script runs.** Every window and tab has an id minted by the platform, and the current list is carried into the agent's context each turn, so there is no listing tool and no round trip to discover them. The one exception is a session's very first turn: reading the screen costs about two seconds the first time a process asks, so a session warms itself in the background instead of making you wait for it, and that turn is told the listing is being read and to ask for it if it needs it. An application is not a target: two Finder windows are two places, and naming the application cannot say which. Every window is listed, including ones behind others, minimized, or on another Space — synthesized input is posted to the process rather than the screen, so an off-screen window is as drivable as any other, and `visible: false` marks it so the agent can say so. Each entry also carries `can`, the vocabulary that place answers to, read off the surface that implements it.

**What changed.** The result reports what each action *changed*, in `changed` — the globals that moved (`title`, `focus`, `selection`, and on a page `url`) and what became newly present. An action that replaced the document reports `navigated` instead of listing a page's worth of elements. An action that changed nothing says `changed: []`, which is the signal that a click missed or a pane had not loaded, rather than that an element was named differently.

**Tabs.** The browser has more than one page, and the script chooses which one it is on:

- `tabs()` lists every open tab as `{id, title, url, active}`.
- `tab(id)` switches to a tab and brings it to the front.
- `new_tab(url)` opens a tab and returns its id.
- `close_tab(id)` closes a tab.

The listing covers **all** of your tabs, not only the ones the agent opened. To filter it would make an ordinary request impossible to serve, such as "the invoice in my other tab". The tool already drives that browser with your logins.

Nothing stops an agent from closing a tab it did not open. It has an instruction instead, in the tool description. These tabs are your working state. To close one can lose a half-filled form, with no undo.

**Frames.** An `iframe` is its own document with its own origin and its own session — the embedded checkout, the OAuth consent screen, the document viewer. Element ids are already frame-scoped, so `f1e3` is the third element of frame `f1` and clicking or typing into it needs no extra step. `frames()` lists them as `{id, url, name, parent, element}`. Then `evaluate(..., frame="f1")` and `read(frame="f1")` run **inside** that document. That is the only way to reach one through the credentials it holds.

Frank attaches to **the Chrome you already use**, with your real logins and sessions, not a throwaway profile. It therefore only ever *connects* to the browser. It never launches it, quits it, or copies it.

Frank reads structure, not pixels: there is no screenshot path for computer use. A drawn surface, such as a canvas or WebGL, exposes nothing to find. A structured visual fallback is planned, but it does not exist yet.

**Enable it:**

- Grant **Accessibility** permission to Frank for native apps (System Settings, then Privacy & Security, then Accessibility). The app prompts you and links directly to the pane. macOS matches the permission to the app's code identity. The packaged build therefore carries a stable identity, which keeps the grant across updates. See the [Development guide](development.md#building-and-signing).
- Turn on Chrome's remote-debugging toggle once for the browser surface. Open `chrome://inspect` and enable it under the remote-debugging option (Frank provides a one-click prompt that opens the page).
- Set `computer_control.enabled: true` in the config (off by default).

> [!NOTE]
> Typing fills a field without submitting unless the agent explicitly asks to — so it never posts a form by accident.

**What counts as changing something.** A script's own calls are read for the primitives that change state: `click`, `type`, `choose`, `upload`, `drag`, `evaluate`, `press`, `navigate`, `new_tab`, `close_tab`, `caret` and `select`. A script that names none of them runs with only the reading primitives in its namespace, so there is nothing to decide. One that names any is put to whoever decides for the session, unless a `screen` rule already names that primitive.

Finding, reading, listing tabs and frames, and switching between tabs are all reads. `evaluate` is on the acting side, because it runs arbitrary JavaScript in a page you are signed in to. `navigate` is there too, because on many sites a URL *is* a command: `/logout`, `/unsubscribe?token=…`, `/items/12/delete`. Nothing that reads primitive names alone can tell those from a page worth reading. In an ordinary session, the harness examines a script that acts; it does not block it. In a [read-only session](configuration.md#permission-modes) it refuses the script outright.

## Where the definitions live

- Descriptions the model reads: the tool docstrings in `src/frank/runtime/tools/registry.py`, and `src/frank/runtime/prompts/tool_*.md` for the peer-session tools
- Implementations: `src/frank/runtime/tools/` and `src/frank/computer/`
- Model-facing message templates: `src/frank/runtime/prompts/` and `src/frank/computer/messages/`
- The guidance a session gets for screen control: `src/frank/runtime/prompts/computer_control_guidance.md`

A tool runs inside the session's own process, so its blast radius is that session. That means its working directory, its permission mode, and its own MCP connections. Under the `worktree` strategy the working directory is the session's own git worktree.
