Create new tasks in the task list. A task can depend on another.

Use this to break complex work into steps that run in parallel or in order. A task with no dependency can start at once. A task with a dependency waits for that dependency to complete.

Keep each task short and factual, and tie it to work somebody can observe. Skip the list for work that your next response finishes. Once you create the list, keep it true to reality with `update_tasks`.

Arguments:
  - tasks: A list of task objects. Each holds:
        - description (required): What somebody must do.
        - dependencies (optional): A list of task identifiers this task waits for, such as ["task-...", ...].
