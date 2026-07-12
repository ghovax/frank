**Call a tool** exposed by a configured MCP server.

Parameters:
- `server` (required): the configured MCP server name (list them with `list_mcp_tools`)
- `tool_name` (required): the tool's name as advertised by `list_mcp_tools`
- `arguments` (optional): JSON object of tool-specific arguments
- `read_only`: set to `true` for inspection-only calls, `false` for calls that modify state (the default when omitted)
- `risk`: set to `"medium"` or `"high"` for calls that modify state

Discover the available `tool_name` and its input `arguments` shape with `list_mcp_tools` first, then call the tool here.

Treat `call_mcp_tool` safety like **bash** safety:
- Set `read_only=true` **explicitly** for **inspection-only** calls — an omitted flag is treated as mutating.
- Set `read_only=false`, with `risk` set to `medium`/`high`, for calls that modify local, remote, account, database, or external state.

MCP tools may return renderable **artifacts** (HTML, iframes, images, links). When modifying an existing artifact, prefer `artifact_update_mode=\"replace\"` / `\"update\"` over creating a duplicate; use `artifact_target_id` to select the artifact to refresh.

Always provide a concise **justification** — written as one smooth, open-ended clause, not a `label: detail` heading.
