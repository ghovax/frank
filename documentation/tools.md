# Tools

Agents act through tools. Every tool call is subject to the [permission engine](configuration.md#permissions); risky ones can pause for approval. Each built-in tool carries the description the model reads as its docstring in `src/daisy/tools/tools.py`, so the authoritative spec is always in the repo.

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
| `spawn_agent` | Delegate a sub-task to another agent. |
| `set_tasks` / `update_tasks` | Maintain a task list for a multi-step job. |
| `update_goal` | Track an overarching goal. |
| `read_task` | Read a related/background task. |
| `load_skill` | Load a `SKILL.md` capability on demand. |
| `open_artifact` | Render a produced file/output as an artifact in the UI. |
| `ask_user` | Ask the user a question and wait. |

**MCP**

`call_mcp_tool`, `list_mcp_tools`, `list_mcp_resources`, `read_mcp_resource` — bridge to any configured [MCP server](agents-and-skills.md#mcp-servers).

## Screen control (`search_screen` + `control_screen`)

Daisy drives the live screen — native macOS apps and **your own Chrome** — through a two-phase pair of tools. These are **macOS-only** and **opt-in**: both are gated by `computer_control.enabled` (the default; see [configuration.md](configuration.md#execution-and-permissions)).

**`search_screen` — read the live surface.** Point it at the current surface (the user's Chrome page or a native macOS app) with a plain-language query, and it returns the matching UI as **ranked elements** to act on, rather than pixels. On native apps it reads the **accessibility tree**; on Chrome it reads the page's real semantic structure (roles and names, iframes included) over the Chrome DevTools Protocol through Playwright. On the browser it additionally surfaces the page's own **network/API requests**, so the agent can see the endpoints the page calls.

**`control_screen` — act on what was found.** Given the elements `search_screen` returned, it composes a short **Python script of trusted-input primitives** — click, type, scroll, `evaluate`, and the like — to carry out the action. On the browser, `evaluate` can **replay the page's own authenticated API in-page**, reusing the real logged-in session instead of re-authenticating. Actions run against the real surface (browser clicks go through Playwright's actionability checks), and the result reports back the resulting state so the agent can see what changed.

Because Daisy attaches to **the Chrome you already use** — your real logins and sessions, not a throwaway profile — it only ever *connects* to the browser: it never launches, quits, or copies it.

**Enable it:**

- Grant **Accessibility** permission to Daisy for native apps (System Settings → Privacy & Security → Accessibility). The app prompts you and links directly to the pane. Because the permission is matched to the app's code identity, the packaged build is signed with a stable identity so the grant survives updates (see [development.md](development.md#building-and-signing)).
- Turn on Chrome's remote-debugging toggle once for the browser surface. Open `chrome://inspect` and enable it under the remote-debugging option (Daisy provides a one-click prompt that opens the page).
- Set `computer_control.enabled: true` (the default) in the config.

> [!NOTE]
> Typing fills a field without submitting unless the agent explicitly asks to — so it never posts a form by accident.

## Where the definitions live

- Descriptions the model reads: the tool docstrings in `src/daisy/tools/tools.py`
- Implementations: `src/daisy/tools/` and `src/daisy/computer/`
- Model-facing message templates: `src/daisy/tools/prompts/` and `src/daisy/core/prompts/`
- The guidance the agent gets for screen control: `src/daisy/core/prompts/computer_control_guidance.md`
