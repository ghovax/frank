## MCP Servers

Configured MCP servers expose external tools and resources (maps, browsers, databases, knowledge stores, charts, …). Discover with `list_mcp_tools`/`list_mcp_resources`, call with `call_mcp_tool` (`server`, `tool_name`, JSON `arguments`), read resources with `read_mcp_resource`. Treat safety like `bash`: `read_only=true` for inspection, `read_only=false` + a `risk` for state changes.

{{ computer_control_guidance }}
