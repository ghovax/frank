Call a tool that a configured MCP server offers.

Find the exact `tool_name` and the schema for `arguments` with `list_mcp_tools` first.

Treat safety as you treat it in `bash`. Always use `access_request` with `mutates: false` for a call that only inspects, and `mutates: true` for one that changes something. An MCP server runs outside this machine's confinement, so a call that changes anything is put to whoever decides for this session unless a rule already names it.

Arguments:
  - server: The name of a configured MCP server.
  - tool_name: The tool name, as `list_mcp_tools` reports it.
  - arguments: A JSON object that matches the MCP tool's input schema.
  - access_request: What this call says about changing anything, and what it needs beyond what the session already holds. Always set `mutates`.
  - explanation: A short reason for the call, in the words the user reads.
