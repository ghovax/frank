List resources exposed by configured MCP servers.

Use this to discover resource URIs before calling ``read_mcp_resource``. Pass a server name to inspect one configured server or leave it empty to inspect every enabled server.

Arguments:
  - server: Optional configured MCP server name. Leave empty to list every enabled server.
  - explanation: A concise, user-facing reason for inspecting resources.
