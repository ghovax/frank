# Tools

Agents act through tools. Every tool call goes through the [permission engine](configuration.md#permission-modes); risky ones can pause for approval. Each built-in tool's docstring in `src/daisy/tools/tools.py` is the description the model reads, so the authoritative spec lives in the repo.

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

## Screen control (`control_screen`)

Daisy drives the live screen — native macOS apps and **your own Chrome** — through one tool, `control_screen`, whose Python script both finds elements and acts on them. It is **macOS-only** and **opt-in**: gated by `computer_control.enabled` (off by default; see [Configuration guide](configuration.md#execution-and-permissions)).

**Finding — read the live surface.** Inside the script, `find_many(query)` and `find_one(query)` take a plain-language query and return the matching UI as **ranked elements** to act on, not pixels — each with a stable `id`, its role, its text, and its context. On native apps this reads the **accessibility tree**; on Chrome it reads the page's real semantic structure (roles and names, iframes included) over the Chrome DevTools Protocol through Playwright, and also surfaces the page's own **network/API requests**, so the agent can find the endpoints the page calls. `find_one` returns the single best match and raises if the top matches are indistinguishable, so an unclear target is caught rather than guessed.

**Acting — a composed script of trusted-input primitives.** The same script drives the elements a find returned (by `id`, or by a query resolved the same way) with **trusted input** — click, type, scroll, `evaluate`, and the like. Because it is ordinary Python, a whole task — loop over rows, branch on what you find, call the page's own API in one line — is a single call, not a round trip per action. On the browser, `evaluate` can **replay the page's own authenticated API in-page**, reusing the logged-in session instead of re-authenticating. Actions run against the real surface (browser clicks go through Playwright's actionability checks), and the result reports what each action touched (`acted_on`) so the agent sees what changed.

Because Daisy attaches to **the Chrome you already use** — your real logins and sessions, not a throwaway profile — it only ever *connects* to the browser: it never launches, quits, or copies it.

Daisy reads structure, not pixels: there is no screenshot path for computer use. A surface that is drawn rather than structured (a canvas, WebGL) exposes nothing to find — a structured visual fallback is planned but not yet built (see [the plan](plans/visual-fallback.md)).

**Enable it:**

- Grant **Accessibility** permission to Daisy for native apps (System Settings → Privacy & Security → Accessibility). The app prompts you and links directly to the pane. The permission is matched to the app's code identity, so the packaged build is signed with a stable identity to keep the grant across updates (see [Development guide](development.md#building-and-signing)).
- Turn on Chrome's remote-debugging toggle once for the browser surface. Open `chrome://inspect` and enable it under the remote-debugging option (Daisy provides a one-click prompt that opens the page).
- Set `computer_control.enabled: true` in the config (off by default).

> [!NOTE]
> Typing fills a field without submitting unless the agent explicitly asks to — so it never posts a form by accident.

## Where the definitions live

- Descriptions the model reads: the tool docstrings in `src/daisy/tools/tools.py`
- Implementations: `src/daisy/tools/` and `src/daisy/computer/`
- Model-facing message templates: `src/daisy/tools/prompts/` and `src/daisy/core/prompts/`
- The guidance the agent gets for screen control: `src/daisy/core/prompts/computer_control_guidance.md`
