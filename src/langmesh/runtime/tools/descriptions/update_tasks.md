Update the status of one or more tasks at once.

Mark a task `in_progress` when the work starts. Mark it `completed` only when it is truly done. Mark it `blocked` where reality stops the work.

Update on a real change of state, never as busy-work. Never end a turn with completed work still shown as unresolved.

Arguments:
  - updates: A list of update objects. Each holds:
        - task_id (required): The task identifier, such as "task-...".
        - status (required): One of 'pending', 'in_progress', 'completed' or 'blocked'.
