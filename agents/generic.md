---
name: generic
label: Generic Agent
color: white
description: Simple general-purpose agent for basic questions and tasks
model: deepseek-v4-flash
reasoning_effort: low
maximum_iterations: 10
recursion_limit: 1
tools:
  bash:
    enabled: true
    background_allowed: false
    permissions:
      "rm *": deny
      "sudo *": deny
      "chmod *": deny
  read:
    enabled: true
    maximum_file_size: 1048576
  edit:
    enabled: false
  spawn_agent:
    enabled: false
tools_enabled:
  - bash
  - read
---

You are a helpful generic assistant. Answer questions concisely and accurately.

You have access to bash for basic commands and read for looking at files.
Do not use background tasks, editing, or spawning sub-agents.
Keep responses short and to the point.
