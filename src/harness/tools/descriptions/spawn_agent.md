**Delegate a task to another agent** (a real A2A call to its endpoint).

**Non-blocking.** The agent runs in the background as a related A2A task; this tool returns immediately with a running handle, and its activity streams live. You do **not** wait on it: its structured deliverable (the completed A2A task with its artifact) is delivered to you automatically when it finishes — the harness re-engages you then, even if your turn has ended in the meantime — exactly like a background command. So spawn, keep working, and pick up each result as it lands.

- **Do not wait for or re-spawn** an agent you already started; it is already running and its result will arrive on its own.
- If its work is no longer needed, pass the returned `agent-...` handle to `cancel_agent`.
- To run **several agents at once**, call this tool multiple times in one response — they run in parallel.
- Give a **self-contained prompt**: the goal, relevant paths, constraints, and the expected return shape (findings, evidence, uncertainty, recommended next action).
- Set `read_only=true` for investigation/research agents that should **report back** rather than make changes.
- Match the task to the right specialist (see `available_agents` in your context).

Do **not** delegate tiny edits, work that needs the same narrow context you already have, or final judgment. Agents provide evidence; **you** decide.
