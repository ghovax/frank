# Tools

Agents act through tools. Every tool call is subject to the
[permission engine](configuration.md#permissions); risky ones can pause for approval. Each
built-in tool ships with a Markdown description the model reads
(`src/harness/tools/descriptions/`), so the authoritative spec is always in the repo.

## The built-in surface

**Shell and files**

| Tool | What it does |
|------|--------------|
| `bash` | Run shell commands. Sandboxed to the workspace by default; per-command rules per agent. |
| `read_file` | Read a file (with line ranges and image support). |
| `write_file` | Create or overwrite a file. |
| `edit_file` | Make a targeted edit to an existing file. |
| `find_files` | Find files by name/glob. |
| `search_content` | Search file contents. |
| `download_file` | Download a file from a URL to disk. |

**Web**

| Tool | What it does |
|------|--------------|
| `web_search` | Search the web (Exa-backed fallback). |
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

`call_mcp_tool`, `list_mcp_tools`, `list_mcp_resources`, `read_mcp_resource` — bridge to any
configured [MCP server](agents-and-skills.md#mcp-servers).

## Computer-use (`computer`)

Controls native macOS apps through the **accessibility tree** — it observes the on-screen
UI as structured elements and acts on them (click, type, key, menu, scroll), falling back
to screenshots when needed. Actions are honest about what actually happened.

**Enable it:** grant **Accessibility** permission to Daisy (System Settings → Privacy &
Security → Accessibility). The app prompts you and links directly to the pane. Set
`computer_control.enabled: true` (the default) in the config.

Because the permission is matched to the app's code identity, the packaged build is signed
with a stable identity so the grant survives updates (see
[development.md](development.md#building-and-signing)).

## Browser (`browser`)

Drives **your own Chrome** — the real browser with your real logins and sessions — rather
than a throwaway automated profile. It connects over the Chrome DevTools Protocol and can
navigate, read the page, click, type, press keys, hover, scroll, manage tabs, and go
back/forward.

**Enable it:** turn on Chrome's remote-debugging toggle once. Open `chrome://inspect`,
enable it under the remote-debugging option (Daisy provides a one-click prompt that opens
the page). No copied profiles, no separate login — Daisy attaches to the Chrome you already
use.

> [!NOTE]
> Typing uses paste-style insertion and does **not** press Enter — the agent presses Enter
> as a separate step, so it never submits a form by accident.

## Where the definitions live

- Descriptions the model reads: `src/harness/tools/descriptions/*.md`
- Implementations: `src/harness/tools/` and `src/harness/computer/`
- The guidance the agent gets for computer/browser control:
  `src/harness/core/prompts/computer_control_guidance.md`
