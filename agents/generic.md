---
name: generic
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
  spawn_agent:
    enabled: false
tools_enabled: []
---

You are a helpful generic assistant. Answer questions concisely and accurately.