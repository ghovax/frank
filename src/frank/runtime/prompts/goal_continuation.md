This goal is not finished:

{{ goal }}

{{ requirements }}

Carry on with it. Look at where the work actually stands before deciding what to do next — the files, the command output, the artifact, as they are now — and take the next step that moves the real end state closer.

## Completion is unproven until you prove it

Do not decide that you are done from your memory of what you did. Take each requirement, name the evidence that would prove it, then go and read that evidence.

- Match the scope of a check to the scope of the claim. A narrow check does not prove a broad claim.
- Treat indirect or uncertain evidence as not met. Get better evidence, or continue the work.
- A green result is evidence only if it covers the requirement. Confirm that it does.

The audit must prove that the work is complete. Finding no obvious work left is not the same thing.

## Do not shrink the goal to fit what you built

A smaller result is not the goal. Nor is a safer one, nor one that is easier to verify, nor one that merely breaks nothing. If the end state is not true, the goal is not met — however good the thing you built is on its own.

## How to resolve it

Call `update_goal`:

- `satisfied` when the evidence proves every requirement. Say what you checked and what it showed.
- `cleared` when the goal no longer applies. Say why.
- `blocked` when the same obstacle has stopped you {{ blocked_turns }} separate times and you cannot go around it without the user or without something outside your reach. Say what is in the way and what would clear it.

Hard is not blocked. Slow is not blocked. Uncertain is not blocked. Unfinished is not blocked. One failure is not an impasse.
