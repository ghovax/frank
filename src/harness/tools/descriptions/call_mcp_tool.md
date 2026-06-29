**Call a tool** exposed by a configured MCP server.

Discover the available `tool_name` and its input `arguments` shape with **list_mcp_tools** first, then call it here.

Treat `call_mcp_tool` safety like **bash** safety:
- Set `read_only=true` for **inspection-only** calls.
- Set `read_only=false`, with `risk` set to `medium`/`high`, for calls that modify local, remote, account, database, or external state.

MCP tools may return renderable **artifacts** (HTML, iframes, images, links). When modifying an existing artifact, prefer `artifact_update_mode="replace"` / `"update"` over creating a duplicate; use `artifact_target_id` to select the artifact to refresh.

Always provide a concise **justification**.
