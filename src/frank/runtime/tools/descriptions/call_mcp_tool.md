Call a tool exposed by a configured MCP server.

Discover the exact ``tool_name`` and ``arguments`` schema with ``list_mcp_tools`` first. Treat safety exactly like ``bash``: set ``read_only=True`` explicitly only for inspection-only calls; omitted means potentially mutating. For state-changing calls, set an appropriate medium or high risk.

Arguments:
  - server: Configured MCP server name.
  - tool_name: Tool name as advertised by list_mcp_tools.
  - arguments: JSON object matching the MCP tool input schema.
  - read_only: Whether this MCP tool call only reads state. Defaults to False (treated as mutating) when omitted.
  - explanation: A concise, user-facing reason for the tool call.
  - risk: One of "low", "medium", "high" for non-read-only calls.
