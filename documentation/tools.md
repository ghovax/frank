# Tools

A session acts through tools. Every tool call goes through the [permission engine](configuration.md#permission-modes); risky ones pause for approval, which reaches you as a prompt in the app or as `xeac approve` in the terminal. The description the model reads is in the repo: a docstring in `src/xeac/runtime/tools/registry.py` for most tools, a template in `src/xeac/runtime/prompts/tool_*.md` for the peer-session ones.

There is no delegation tool and no in-process sub-agent. A session that needs a peer creates one with `create_session`, which reaches the same control plane your terminal does. See [Architecture](architecture.md#sessions).

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

**Web**

| Tool | What it does |
|------|--------------|
| `search_web` | Search the web (Exa-backed fallback). |
| `fetch_url` | Fetch and read a page via a tiered engine (Jina → Firecrawl → direct). |

**Orchestration and knowledge**

| Tool | What it does |
|------|--------------|
| `set_tasks` / `update_tasks` | Maintain a task list for a multi-step job. |
| `update_goal` | Track an overarching goal. |
| `read_turn` | Read a sibling turn handed to this session from outside. |
| `load_skill` | Load a `SKILL.md` capability on demand. |
| `open_artifact` | Render a produced file or output as an artifact in the UI. |
| `ask_user` | Ask the user a question and wait for the answer. |
| `wait_for` | Pause for a few seconds without a model round trip, to re-check something that was not ready. |

**Peer sessions**

| Tool | What it does |
|------|--------------|
| `create_session` | Create a peer session and give it work. Its `agent` argument enumerates the profiles actually installed, so an unknown name cannot be asked for. Returns as soon as the peer exists. |
| `send_message` | Send a message to another session — one you created, or the one that created you. Delivered into its current turn if it is already working, rather than queued behind it. |
| `read_session` | One session's state: its profile, whether its process is alive, whether a turn is in flight, whether it is waiting on a human. |
| `list_sessions` | The sessions this one created. Its own subtree, not the machine's. |
| `end_session` | End a peer and everything under it. |

The caller is the parent, always — it is not an argument. That is what puts a peer inside the tree, inside the reaper, and under the permission clamp, so a peer can never hold authority the session that made it does not have.

**A peer answers by messaging.** When it is done it calls `send_message` on the session that created it, whose id is in its context as `parent_session`, and that message lands in the caller's context the way any inbound message does. So `create_session` does not wait, there is no handle to hold, and nothing reconstructs a result: the peer decides what its answer is, in its own words, at the moment it knows. A caller starts the work, carries on with whatever does not depend on it, and ends its turn — the reply wakes it.

That message arrives as a **peer turn**, not a user turn. The distinction is carried on the wire (`xeacTurnKind`, plus `xeacPeerSender` naming the sender) because without it a peer's report would reach the model as an instruction from the person it works for, and would appear in the transcript as words the user never wrote.

A peer that dies before reporting cannot say so, which is the one thing the harness says on its behalf: the daemon tells the parent when it reaps a child, with the child's id and why it ended.

**Agents on other hosts**

`list_remote_agents` and `send_to_remote_agent` — separate verbs, because a remote agent is a separate bargain: someone else's machine, someone else's cost, no shared history, and no access to this filesystem. Present only when one is registered.

**MCP**

`call_mcp_tool`, `list_mcp_tools`, `list_mcp_resources`, `read_mcp_resource` — bridge to any configured [MCP server](agents-and-skills.md#mcp-servers).

## Screen control (`control_screen`)

XEAC drives the live screen — native macOS apps and **your own Chrome** — through one tool, `control_screen`, whose Python script both finds elements and acts on them. It is **macOS-only** and **opt-in**: gated by `computer_control.enabled` (off by default; see [Configuration guide](configuration.md#execution-and-permissions)).

**Finding — read the live surface.** Inside the script, `find_many(query)` and `find_one(query)` take a plain-language query and return the matching UI as **ranked elements** to act on, not pixels — each with a stable `id`, its role, its text, and its context. On native apps this reads the **accessibility tree**; on Chrome it reads the page's real semantic structure (roles and names, iframes included) over the Chrome DevTools Protocol through Playwright, and also surfaces the page's own **network/API requests**, so the agent can find the endpoints the page calls. `find_one` returns the single best match and raises if the top matches are indistinguishable, so an unclear target is caught rather than guessed.

**Acting — a composed script of trusted-input primitives.** The same script drives the elements a find returned (by `id`, or by a query resolved the same way) with **trusted input** — click, type, scroll, `evaluate`, and the like. Because it is ordinary Python, a whole task — loop over rows, branch on what you find, call the page's own API in one line — is a single call, not a round trip per action. On the browser, `evaluate` can **replay the page's own authenticated API in-page**, reusing the logged-in session instead of re-authenticating. Actions run against the real surface (browser clicks go through Playwright's actionability checks), and the result reports what each action touched (`acted_on`) so the agent sees what changed.

Because XEAC attaches to **the Chrome you already use** — your real logins and sessions, not a throwaway profile — it only ever *connects* to the browser: it never launches, quits, or copies it.

XEAC reads structure, not pixels: there is no screenshot path for computer use. A surface that is drawn rather than structured (a canvas, WebGL) exposes nothing to find — a structured visual fallback is planned but not yet built (see [the plan](plans/visual-fallback.md)).

**Enable it:**

- Grant **Accessibility** permission to XEAC for native apps (System Settings → Privacy & Security → Accessibility). The app prompts you and links directly to the pane. The permission is matched to the app's code identity, so the packaged build is signed with a stable identity to keep the grant across updates (see [Development guide](development.md#building-and-signing)).
- Turn on Chrome's remote-debugging toggle once for the browser surface. Open `chrome://inspect` and enable it under the remote-debugging option (XEAC provides a one-click prompt that opens the page).
- Set `computer_control.enabled: true` in the config (off by default).

> [!NOTE]
> Typing fills a field without submitting unless the agent explicitly asks to — so it never posts a form by accident.

## Where the definitions live

- Descriptions the model reads: the tool docstrings in `src/xeac/runtime/tools/registry.py`, and `src/xeac/runtime/prompts/tool_*.md` for the peer-session tools
- Implementations: `src/xeac/runtime/tools/` and `src/xeac/computer/`
- Model-facing message templates: `src/xeac/runtime/prompts/` and `src/xeac/computer/messages/`
- The guidance a session gets for screen control: `src/xeac/runtime/prompts/computer_control_guidance.md`

A tool runs inside the session's own process, so its blast radius is that session: its working directory (its own git worktree, under the `worktree` strategy), its permission mode, and its own MCP connections.
