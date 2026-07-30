Set, replace, satisfy, or clear the single active goal for this turn.

A goal is not a task list. It is the top-level completion contract the harness injects back into your context until you explicitly satisfy or clear it. Use it when a user request has a concrete outcome that must not be lost while you run tools, hand work to a peer session, or continue across multiple model passes. Do not set a goal for a tiny one-shot answer. While a goal is active, keep working until it is satisfied, explicitly clear it if it becomes obsolete, or leave it active only when work genuinely remains.

Arguments:
  - goal: The goal text to set when status is "active". Leave empty when marking the current goal as "satisfied" or "cleared".
  - status: "active" sets/replaces the goal, "satisfied" removes it because the requested outcome is done, and "cleared" removes it because it is obsolete or no longer applicable.
  - explanation: A concise, user-facing reason for this update.
