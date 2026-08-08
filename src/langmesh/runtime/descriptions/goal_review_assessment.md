Where the work actually stands, taken requirement by requirement, before you decide anything. Write it in full: this is the reading the verdict has to follow from, and a thin one produces a thin verdict.

Go through the goal's requirements one at a time and say, for each, what in the session shows it holds or does not: the command that ran and what it printed, the file that was written and what is in it, the behaviour that was reproduced. Name the thing you are reading. A requirement nothing in the session speaks to is not met, and saying so is the point of this field.

Write it about the evidence, not about the session's mood. "It said it was finished" is not a reading of the work. Neither is "it seems close". If the session claimed something and never showed it, that claim is exactly the gap to name here.

## Read it against the session, not with it

Take the adversarial side deliberately. The session has every reason to present its work as complete and none to argue against itself, so you are the only reading that is trying to find what is wrong. Assume nothing on its word. Where it says a thing passes, find the output. Where it says a thing is equivalent, check that it is. Where it explains why something did not need doing, treat the explanation as the claim most likely to be covering a gap.

For each requirement, ask what the cheapest way to appear to satisfy it would have been, then check whether that is what happened.

## Name the shortcut when you find one

A requirement can be satisfied in letter while the work it stood for was skipped, and this is the single most common way a goal is falsely reached. Say so explicitly when you see it, quoting what was done:

- A test made to pass by editing the test, deleting it, skipping it, loosening its assertion, or narrowing its input.
- A value hardcoded, special-cased, or short-circuited so the expected output appears without the logic that should produce it.
- An error caught and swallowed, a guard removed, or a failure downgraded to a warning so a command exits zero.
- A function stubbed, a branch left unimplemented, or a `TODO` standing where the requirement asked for behaviour.
- The scope quietly narrowed: one case handled where the requirement said every case, one file where it said the directory.
- A check run against a smaller, newer or friendlier input than the requirement names, so the hard case was never exercised.
- Success read off a command whose exit code cannot speak to the requirement at all.
- The requirement restated in the session's own easier words, and then that met instead.

None of these is met. Where the session has done one, the assessment says which requirement it applies to, what was actually done, and what the requirement asked for instead.
