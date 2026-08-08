What the session does next, written to the session as the message that opens its next turn. Required unless the goal is satisfied.

Write it in the second person, as an instruction, not as a report about the session. It is the only thing the session receives from you, so it has to carry everything it needs: which requirement is being worked, what is already known about it from this session's own work, and the concrete next action — the command to run, the file to open, the thing to check.

Be specific enough that it could not have been written about a different goal. "Continue working toward the goal" is worthless. "Requirement two is unproven: you changed the parser but never ran it against the fixture in `tests/data`; run it and read the output" is the shape.

When the same route has failed more than once, do not send the session down it again. Name a different one and say why it is different. If the session has been reading, tell it to run something; if it has been running one command against one input, tell it to widen or change the input; if it has been editing, tell it to check what it edited. The session's own account of being stuck is a description of one route, never of every route.

Where a requirement is proven, say so plainly, so the session does not spend the turn re-proving what is already established.
