**Set, replace, satisfy, or clear** the single active goal for this turn.

A goal is **not** a task list. It is the top-level *completion contract* the harness injects back into your context until you explicitly satisfy or clear it. Use it when a request has a concrete outcome that must not be lost while you run tools, delegate, or continue across multiple model passes.

- `status="active"` sets or replaces the goal.
- `status="satisfied"` removes it because the requested outcome is **done**.
- `status="cleared"` removes it because it is **obsolete** or the user changed direction.

Do **not** set a goal for tiny one-shot answers. While an active goal is present, do **not** end the turn casually — satisfy or clear it first, or keep working.

Always provide a concise **justification**.
