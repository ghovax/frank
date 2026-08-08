## MCP Servers

A configured MCP server offers external tools and resources: maps, browsers, databases, knowledge stores, charts, and more.

- Discover what a server has with `list_mcp_tools` and `list_mcp_resources`.
- Call a tool with `call_mcp_tool`, which takes `server`, `tool_name` and a JSON `arguments` object.
- Read a resource with `read_mcp_resource`.

Treat safety here as you treat it in `bash`. State what the call does with `access_request`: `mutates: false` for a call that only inspects, and `mutates: true` for a call that changes something.
