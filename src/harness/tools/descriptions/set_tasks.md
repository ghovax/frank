**Create tasks** in the task list to lay out a multi-step plan.

Each task is one concrete step; wire the order with `dependencies` (a step lists the task ids it waits on). Tasks with no dependencies can start immediately; tasks with dependencies wait for them to complete first.

Keep entries **short, factual, and tied to observable work**. Skip the list entirely for a request the next response can obviously finish. **A task list you don't maintain is worse than none** — once you create tasks, keep them reconciled to reality with `update_tasks`.
