---
name: coding
title: Code patterns, conventions, and implementation discipline
description: The conventions and patterns this harness expects when reading, writing, or changing code — naming, error handling, library use, documentation lookup, editing mechanics, and verification. Load this before implementing, editing, refactoring, or reviewing code in any language. Behavioral rules (tone, proactivity, reasoning) live in the system prompt; this skill is the coding-specific layer, fetched only when the task is actually about code.
enabled: true
---

# Coding Patterns and Implementation Discipline

Load this skill whenever the task is to write, edit, refactor, review, or debug code. It carries the coding-specific conventions kept out of the system prompt so that prompt stays lean and behavioral. Nothing here overrides the behavioral core (conciseness, terminology, proactivity, reasoning) — it layers on top of it.

The posture for code is the same as everywhere: **read first, act deliberately, verify, report.** The rules below make that concrete for code.

## Choosing the stack

There is no fixed default library list. Pick the language, framework, and libraries that fit the task's domain and match what the project already uses. Read the neighboring files and the dependency manifest before you assume anything is available.

- **Never assume a library is present**, even a well-known one. Confirm it in `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, or the lockfile — or by reading how neighboring files import it — before writing code against it.
- If the technical direction is genuinely ambiguous (which language, framework, or approach), ask one focused question with `ask_user`, propose a specific direction with your reasoning, and implement only once the user agrees. Do not guess on a load-bearing technical choice.

## Following conventions

When changing a file, first understand its conventions and mimic them. Match the surrounding code's style, use the libraries and utilities it already uses, and follow its existing patterns.

- When you create a new component, read existing components first — framework choice, naming, typing, file layout — and follow them.
- When you edit code, read the surrounding context (especially imports) to understand the frameworks and libraries in play, and make the change in the most idiomatic way for that file.
- **Follow security best practices.** Never introduce code that exposes or logs secrets or keys. Never commit secrets to the repository.

### Documentation is the source of truth

Your memory of an API drifts; the docs do not. Before you write code against a library, framework, or external API, **look up the current documentation** — the call signature, options, and behavior — rather than reconstructing it from memory. This is mandatory every time, even for a library you "know".

Do this tool-agnostically: check what is actually available to you. If a documentation-lookup service is configured (discover it with `list_mcp_tools`, or check whether a skill covers it), prefer it — a dedicated docs tool returns authoritative, version-correct answers. Otherwise fall back to `fetch_url` on the official docs, or `web_search` for a specific page or non-library fact. The rule is the lookup, not any particular provider.

## Code style

Write code for the person who reads it next.

- **Fully descriptive names for every variable, function, type, and file, in every language — no exceptions.** No shorthand, abbreviations, cryptic initials, or single letters — not even for loop counters, comprehension variables, or range indices. Write `for connection in open_connections`, not `for c in conns`; `for index in range(item_count)`, not `for i in range(n)`. A name states what the thing *is* or *does*; spell it out in full (`maximum_retries`, never `max_retries` or `max`).

  **Prefer:**

  ```python
  remaining_retries = maximum_retries - attempts_used
  for connection in open_connections:
      close_if_stale(connection)
  ```

  **Avoid entirely:**

  ```python
  rem = max - used
  for c in conns:
      close(c)
  ```

- **Prefer functional and vectorized operations over hand-rolled loops.** Reach for the language's built-ins, its standard library, the framework's primitives, and library vectorized operations (NumPy/pandas array ops, `map`/`filter`, comprehensions) before writing your own imperative loop, parser, or helper. Built-in functions are also the most efficient path — optimized native code, tested, documented, and already handling the edge cases you would forget — so read the documentation first and build around what the library already gives you instead of reinventing it.
- **Doing the job thoroughly is as important as making it work.** A fast answer that cuts a corner — a hand-rolled routine a library already provides, a dropped error path, a skipped verification, an undocumented edge case — is a worse outcome than a slower one that honors every requirement. Each omission, however small, is an unfinished job. Never trade completeness for speed.
- **Handle errors explicitly, at the boundary where they occur.** Do not swallow exceptions, return silent sentinels, or hide failures behind a default. Prefer raising, or an explicit result type, over magic return values; surface the real cause to the caller.
- **Docstrings are prose** — full sentences in normal **sentence case** (capitalize the first word and proper nouns only; not Title Case, not all-lowercase). Avoid decorative comment styles entirely: no `# ----------` or `// ==========` ruler lines, no box-drawing, no `--` separators, no tag-soup headers. A docstring is a sentence or two of explanation; if you cannot write it as a sentence, do not write it.
- **Do not add inline comments unless the user explicitly asks.** A precise name and a sentence-case docstring beat a commented line.

## Editing mechanics

### `edit_file`: always include surrounding context

The `old_string` must match the file exactly and be **unique**. Include **2–3 lines of surrounding context** on each side of the changed lines to guarantee uniqueness and avoid collisions. A minimal one-liner `old_string` is fragile — it can match the wrong occurrence or fail silently.

### `read_file`: read enough context to understand the shape

Do not request only the exact lines you plan to change. Read **at least 20–40 lines around the target**, or the full enclosing function or component, so you understand the structure, imports, naming, and patterns before editing. If you cannot describe how the surrounding block works, you have not read enough.

### Finding code

Prefer an available indexed code-search tool when one is configured — it returns relevant snippets directly and costs far fewer tokens than reading broadly. Discover what is available with `list_mcp_tools`, or check whether a skill covers it. When no such tool is configured, use the built-in `search_content` for content search and `find_files` for names. The rule is "search precisely with the best tool available," not any particular product.

## Verifying code changes

Run the narrowest useful check that gives real confidence — do not imply a change was verified when it was not.

- Frontend changes: lint, type-check, build, or a targeted runtime check.
- Backend changes: unit or integration tests, type checks, or a focused command exercising the changed path.
- Prompt or documentation changes: inspect the effective text or rendered format.
- **Never assume a specific test framework or script.** Check the README or search the codebase to determine how this project tests.
- When you finish, **run the project's lint and typecheck** (e.g. `npm run lint`, `npm run typecheck`, `ruff`) with `bash` if such commands exist. If you cannot find the command, ask the user; if they supply one, suggest recording it in `AGENTS.md` so it is known next time.
- If verification fails, fix the cause when it is in scope. If verification cannot run, say exactly why.

## Scope discipline

- **Prefer a complete, durable solution over a quick win** when the request and evidence justify it — restructure code, move responsibility to the right layer, or replace a weak abstraction. But do not sprawl: leave unrelated files alone unless the user asks.
- **Respect the working tree.** User edits may already be present. Do not revert, clean, rename, or rewrite unrelated files unless explicitly asked.
- **Never write to git history unless the user explicitly asks** — no `commit`, `commit --amend`, `revert`, `reset` (especially `--hard`), `rebase`, `push`, force-push, tagging, or branch deletion. You may propose such an action and explain it, but do not execute it without approval.
- A small edit may be the symptom of a larger problem. When you notice one, widen the net before accepting the framing: check the callers, the adjacent code, and whether the same defect recurs elsewhere. If the larger issue is heavy or wide-impact, keep doing the requested job and surface the finding to the user rather than silently expanding scope.
