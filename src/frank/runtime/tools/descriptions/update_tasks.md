Update the status of one or more tasks at once.

Mark a task ``in_progress`` when work starts, ``completed`` only when it is actually done, and ``blocked`` when reality prevents progress. Update on real state changes—not as busy-work—and never end with completed work still shown as unresolved.

Arguments:
  - updates: List of update objects. Each object has:
        - task_id (required): The task identifier (e.g. "task-...").
        - status (required): One of 'pending', 'in_progress', 'completed', 'blocked'.
