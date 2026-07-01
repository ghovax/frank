**Update the status** of one or more tasks at once.

For each update, move the task to `in_progress` when you start it, `completed` when it is **actually done**, and `blocked`/`cancelled` when reality diverges from the plan.

Each update object takes:
- `task_id` (required): the task identifier (e.g. `"task-1"`)
- `status` (required): one of `"pending"`, `"in_progress"`, `"completed"`, `"blocked"`

- Do **not** leave tasks sitting in their initial state while you finish the work around them.
- **Never** end the turn with steps still unresolved that you in fact completed \u2014 reconcile first.irst.
- Update on **genuine state changes**, not as busy-work.
