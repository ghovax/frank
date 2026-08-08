What proves the goal, requirement by requirement. Required when the standing is `satisfied`, and left empty otherwise.

For each requirement, name what was looked at and what it showed. This is what the person reads to decide whether to trust that the goal was reached, so it has to survive being checked: a command and its output, a path and what it contains, a behaviour and how it was reproduced. Quote the output rather than characterising it — "printed `14 passed`" is evidence, "the tests were fine" is not.

A requirement you cannot write a line like that for is a requirement that is not proven, which means the standing is not `satisfied`.

State the evidence in the requirement's own terms. A proxy the session chose for itself does not count: a suite that passes because the failing case was removed from it proves nothing about the case, a count taken from the code that produced it is not a measurement, and a command that exits zero proves only that it exited zero. If the proof rests on something the session changed in order to make the proof possible, say so — and then it is not proof.
