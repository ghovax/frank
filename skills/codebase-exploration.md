---
name: codebase-exploration
label: Codebase exploration
description: Investigate a codebase and explain how it works, citing exact files and line numbers, without modifying anything.
---

# Codebase exploration

Use this skill to understand and explain code.

1. **Map before reading.** Use `grep`/`rg` and `find`/`fd` to locate the relevant files and symbols before opening anything in full.
2. **Follow the call graph.** Trace how data flows — entry point → handler → helpers → storage — rather than reading files in isolation.
3. **Read the real code.** Confirm behaviour in the source; don't infer it from names. Read tests to learn intended behaviour.
4. **Cite precisely.** Reference findings as `path/to/file.py:line` so they can be clicked through.
5. **Explain, don't dump.** Summarise how it works and why, with the citations as evidence — not a wall of pasted code.
