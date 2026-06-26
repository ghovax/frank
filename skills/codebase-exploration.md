---
name: codebase-exploration
label: Codebase exploration
description: Investigate a codebase and explain how it works, citing exact files and line numbers, without modifying anything.
---

# Codebase Exploration

Use this skill when the task is to understand, explain, or diagnose a codebase without modifying it. The goal is not to read a lot of files; the goal is to build a defensible model of how the relevant behavior actually works.

## Workflow

1. **Map before reading deeply.** Use `rg`, `rg --files`, `find`, or `fd` to locate the relevant files and symbols before opening anything in full. This avoids anchoring on the first plausible file.
2. **Follow the real execution path.** Trace data and control flow across boundaries: entry point, handler, helper, persistence, side effect, and caller-visible result.
3. **Read tests and configuration.** Tests often reveal intended behavior; configuration often explains why code paths differ across environments.
4. **Separate facts from inference.** If you infer behavior from structure rather than observe it directly, say so. This prevents a useful hypothesis from being mistaken for proof.
5. **Cite precisely.** Reference important findings as `path/to/file.py:line` so the parent agent or user can inspect the evidence quickly.

## Output Shape

Lead with the direct answer. Then explain the path that supports it, using short citations instead of pasted code. End with open questions only when they materially affect confidence or next steps.
