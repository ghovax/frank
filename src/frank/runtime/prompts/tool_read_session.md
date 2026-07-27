Read one session's current state: the agent profile it runs, whether its process is alive, whether a turn is in flight, and whether it is parked waiting on a human.

This is for orientation, not for waiting. Never call it in a loop to find out whether a peer has finished — a peer's result is delivered to you on its own.
