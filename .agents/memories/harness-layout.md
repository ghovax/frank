---
name: harness-layout
title: Harness configuration layout
importance: high
tags: configuration, dotagents, mcp
---

This project uses the .agents protocol layout. Sub-agent profiles live in .agents/agents/<name>/agent.md with runtime settings in config.json. Skills live in .agents/skills/<name>/SKILL.md. MCP servers are configured in .agents/mcp.json; non-trivial local MCP examples live in examples/mcp/<server-id>/ with server.py and sibling templates/assets.
