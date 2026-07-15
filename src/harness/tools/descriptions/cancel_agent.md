Cancel one running agent using the exact `agent-...` task identifier returned by `spawn_agent`.

Use this when an agent's work is no longer needed, has been superseded, or should be stopped before it finishes. Cancellation is targeted: other spawned agents and intentionally backgrounded shell or search jobs continue running. The agent lane is settled as canceled and no completion wake is emitted for the canceled work.
