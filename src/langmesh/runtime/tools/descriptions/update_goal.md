Set, satisfy, clear or report the session's single goal.

A goal is not a task list. The task list is the steps; the goal is the outcome those steps are for, and it stays until the outcome is real. Set one where the user asks for a concrete end state that will take several calls, edits or checks to reach. Skip it for a small one-shot answer.

Setting a goal is a claim about what "done" means, so state it well enough that somebody else could check it. `goal` is the end state in one sentence. `requirements` are the conditions that must hold for that sentence to be true, each one something you can actually go and look at — a command that passes, a file that exists and says a particular thing, a behaviour you can reproduce. Vague requirements make a goal that cannot be audited, and one that cannot be audited is one you will end up calling done from memory.

While a goal is set, work toward it. Mark it `satisfied` only once you have checked the requirements against the current state and can say what proved each one — not from your recollection of having done the work. Mark it `blocked` when the same obstacle has stopped you repeatedly and you cannot go around it without the user or something outside your reach; hard, slow and unfinished are none of them blocked. Mark it `cleared` when the outcome stopped being what the user wants.

Arguments:
  - status: "active" sets or replaces the goal. "satisfied" ends it because the outcome is real. "blocked" reports an impasse and leaves the goal standing. "cleared" ends it because it no longer applies.
  - goal: The end state, in one sentence. Required for "active".
  - requirements: The conditions that must hold, each checkable. Required for "active".
  - evidence: What you checked and what it showed, for each requirement. Required for "satisfied".
  - blocker: What is in the way, and what would clear it. Required for "blocked".
  - explanation: A short reason for the update, in the words the user reads.
