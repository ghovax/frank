Create new tasks in the task list. Tasks can depend on each other.

Use this to break down complex work into steps that can run in parallel or sequentially. Tasks with no dependencies can be worked on immediately. Tasks with dependencies must wait for their dependencies to complete first. Keep tasks short, factual, and tied to observable work. Skip the list for work the next response can plainly finish; once created, keep it reconciled with reality through ``update_tasks``.

Arguments:
  - tasks: List of task objects. Each object has:
        - description (required): What needs to be done.
        - dependencies (optional): List of task identifiers this task depends on (e.g. ["task-...", ...]).
