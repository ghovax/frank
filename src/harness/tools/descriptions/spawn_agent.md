**Delegate a task to another agent** (a real A2A call to its endpoint).

The sub-agent runs as a related A2A task in the same context. Its activity streams live, and its structured deliverable (the completed A2A task with its artifact) is returned as this tool's result, so you can read it and decide what to do next — including spawning further agents that build on it.

- To run **several agents at once**, call this tool multiple times in one response.
- Give a **self-contained prompt**: the goal, relevant paths, constraints, and the expected return shape (findings, evidence, uncertainty, recommended next action).
- Set `read_only=true` for investigation/research sub-agents that should **report back** rather than make changes.
- Match the task to the right specialist (see `available_agents` in your context).

Do **not** delegate tiny edits, work that needs the same narrow context you already have, or final judgment. Sub-agents provide evidence; **you** decide.

The `justification` is shown directly as the label for this call — make it concise and user-facing.
