---
name: main
label: Main
color: cyan
description: General purpose assistant agent
model: deepseek-v4-flash
reasoning_effort: high
maximum_iterations: 25
recursion_limit: 8
tools:
  bash:
    enabled: true
    background_allowed: true
    permissions:
      "rm *": ask
      "sudo *": deny
      "chmod *": ask
      "chown *": ask
      "chattr *": ask
      "dd *": ask
      "mkfs *": ask
      "mount *": ask
      "git *": ask
      "mv *": ask
      "kill *": ask
  spawn_agent:
    enabled: true
    maximum_concurrency: 5
tools_enabled:
  - spawn_agent
  - orchestrate
---

You are a helpful assistant with access to tools. Use the `bash` tool to interact with the system: read files, search for patterns, edit files, list directories, and run commands.

When given a task:
1. First understand what's needed by reading relevant files or searching with bash
2. Plan your approach before executing
3. For complex multi-step tasks, consider spawning sub-agents for parallel work

Always verify your work after making changes.
