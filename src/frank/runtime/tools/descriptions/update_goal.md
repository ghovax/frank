Set, replace, satisfy or clear the single active goal for this turn.

A goal is not a task list. It is the top-level contract for completion, and the harness injects it back into your context until you satisfy it or clear it.

Use a goal where the user's request has a concrete outcome that must survive while you run tools, hand work to a peer session, or continue across several model passes. Do not set a goal for a small one-shot answer.

While a goal is active, keep working until it is satisfied. Clear it where it stops applying. Leave it active only where work genuinely remains.

Arguments:
  - goal: The goal text, when status is "active". Leave it empty when you mark the current goal "satisfied" or "cleared".
  - status: "active" sets or replaces the goal. "satisfied" removes it because the outcome is done. "cleared" removes it because it no longer applies.
  - explanation: A short reason for the update, in the words the user reads.
