List tools exposed by configured MCP servers.

Use this to discover the exact tool name and input schema before calling ``call_mcp_tool``. Pass a server name to inspect one configured server or leave it empty to inspect every enabled server.

Arguments:
  - server: Optional configured MCP server name. Leave empty to list every enabled server.
  - explanation: A concise, user-facing reason for inspecting MCP tools.
