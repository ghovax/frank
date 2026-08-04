Call a tool that a configured MCP server offers.

Find the exact `tool_name` and the schema for `arguments` with `list_mcp_tools` first.

Treat safety as you treat it in `bash`. Use `access_request` with `mutates: false` for a call that only inspects. Use `mutates: true` and a matching `risk` for a call that changes something.

Arguments:
  - server: The name of a configured MCP server.
  - tool_name: The tool name, as `list_mcp_tools` reports it.
  - arguments: A JSON object that matches the MCP tool's input schema.
  - access_request: What this call needs beyond what the session already holds. It must set `mutates` when present.
  - explanation: A short reason for the call, in the words the user reads.
  - risk: One of "low", "medium" or "high", for a call that changes something.
