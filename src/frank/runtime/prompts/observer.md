# Condense conversation history into memory

The older turns of a conversation below are about to leave the active context window. Record what happened as compact, structured memory so those turns can be dropped without losing anything the agent will need later. Capture:

- **decision** — a choice or approach the agent committed to, and why.
- **fact** — something learned about the codebase, the system, or the world.
- **artifact** — a file, path, or resource created or modified (record the exact path).
- **goal** — the user's objective, preference, or constraint.
- **open** — an unfinished thread or an agreed next step.

Guidance:

- Keep each observation terse and information-dense — record outcomes and state, not verbatim wording. Expect a 6–40× reduction from the raw messages.
- Preserve concrete identifiers (paths, ids, names, commands, numbers) exactly.
- Do **not** duplicate anything already in the existing memory shown below; record only what is new.

## Existing memory

```json
{{ existing_observations }}
```
