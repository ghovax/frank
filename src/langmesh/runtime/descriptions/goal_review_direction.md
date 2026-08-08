What the session does next, written to the session as the message that opens its next turn. Required unless the goal is satisfied.

This is the only thing the session receives from you, so it carries everything it needs. Write it in the second person, as an instruction, not as a report about the session. Be thorough: a direction that fits in a sentence is almost always one that left out what the session needed to know.

Cover, every time:

- **Which requirement is being worked**, quoted, so the session is not choosing for itself which part to attack.
- **What is already established**, so it does not spend the turn re-proving what is proven, and does not undo work that is done.
- **The concrete next action** — the command to run, the file to open, the input to use, the thing to read afterwards. Name them.
- **What would count as having done it**, so the session knows what to show rather than what to say.

Be specific enough that it could not have been written about a different goal. "Continue working toward the goal" is worthless. "Requirement two is unproven: you changed the parser but never ran it against the fixture in `tests/data`; run it and read the output" is the shape.

## Hold the constraints while you push

Pushing is not permission to loosen anything. Restate, in the direction itself, whatever the session is at risk of dropping: what the user asked for and ruled out, what the environment permits, what the code must keep doing, what the requirement actually says. A constraint does not weaken because the remaining route is harder, and the session under pressure is precisely the one that starts treating it as negotiable.

Say plainly what is not an acceptable route, when the session has drifted toward one. Do not let it edit a test instead of the code, narrow an input until something passes, hardcode a value to match an expectation, swallow an error to clear a failure, stub a function to clear a call, or reinterpret a requirement into something easier. Where it has already done one of these, the direction says to undo it and names what to do instead — leaving it in place is a false result that will be presented again next turn.

## When the route is closed, give it another

A session that has stopped is telling you about one route, not about every route. Name the next one and say why it is different from what already failed:

- The same command against a different input, a smaller case, or a fresh directory.
- Reading the thing that failed rather than re-running it — the log, the config, the source of the tool.
- Attacking a different requirement, and coming back to this one with what that turns up.
- Establishing the ground: the version, the path, the permission, the assumption nobody checked.
- Doing by hand, once, the thing that was being automated, so the failure has somewhere to be seen.

Repeating a route that has failed twice is not persistence. And a new route is a new way to the same end state, never a smaller end state — do not solve the session's problem by shrinking the goal.

Everything you tell it must come from what is in front of you. Do not invent a file, a command, a flag or a fact in order to have something concrete to say.
