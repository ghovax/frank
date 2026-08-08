Set the session's single goal: the outcome the work is for, and what would prove it reached.

A goal is not a task list. The task list is the steps; the goal is the outcome those steps are for, and it stays until the outcome is real. Set one where the user asks for a concrete end state that will take several calls, edits or checks to reach. Skip it for a small one-shot answer.

Setting a goal is what keeps the session working past the end of a turn, so set it early — at the point you understand what is being asked, not once you are nearly done.

## Deciding where the goal stands is not yours

You set the goal. You do not close it. Whether it is reached is read off the work by a separate review that sees this session and judges it against what you wrote here, and what it decides is what happens next: another turn with an instruction it writes for you, or an end. There is no argument to make to it and no call that ends a goal early.

Two things follow. Write the goal for that reader, since it has only your words and this session to go on — a requirement it cannot check is one that will never be marked met. And do not spend a turn declaring yourself finished: state what you did and what it showed, and let it be read.

Replacing a goal is for a goal that changed, not for one that got hard. A smaller goal set to be done with a larger one is the one misuse of this tool.

## What a goal's status means

The goal you are shown carries a status once it is anything other than being worked. You did not set it and cannot change it; it is there so you know where you stand.

- `active` — being worked. This is the ordinary state and is not shown to you.
- `satisfied` — the review found every requirement proven. The work is done; do not start it again.
- `blocked` — the review found no route open from inside this session. The `blocker` beside it says what a person would have to do. Nothing further opens on its own.
- `parked` — the goal ran as many turns unattended as it is allowed and stopped to wait. Not a judgement about the work: it picks up where it left off when somebody speaks.
- `cleared` — the person called it off. It is no longer what they want, whatever state the work is in.

A goal that is satisfied, blocked, cleared or parked is not one to keep working at. If the conversation moves on to something that needs a new outcome, set a new goal.

## Writing it well

`goal` is the end state, in one or two sentences, written as something that is either true or not — "the importer handles paginated responses and the existing tests still pass", not "work on the importer". Somebody who has not read this conversation should be able to tell from it alone what would count as done.

`purpose` is what that end state is for: the reason the user wants it, the problem it solves, the thing that would still be wrong if it were skipped. It is what lets a closed route be told apart from a lost goal — a reviewer who knows what the outcome is for can send you down a different road to the same place, and one who does not can only ask you to try the same road again. Write the need, not a restatement of the goal.

`requirements` are the conditions that must hold for the goal to be true, each one something a reader could go and look at: a command that passes, a file that exists and says a particular thing, a behaviour that can be reproduced, a number that lands inside a range. Say what would be looked at and what it would show. Cover the whole of the goal — a requirement missing from this list is a part of the outcome nobody will check — and keep them independent, since one condition that folds three things together cannot be half met.

Vague requirements make a goal that cannot be audited. "Tests pass" names no tests; "it works" names no behaviour; "the code is clean" names nothing at all. A goal like that either never closes or closes on somebody's impression.

Write all three in the language the user is speaking: the goal is shown to them, above where they type.

Arguments:
  - goal: The end state, written so it is either true or not.
  - purpose: What that end state is for, in the user's terms.
  - requirements: The conditions that would prove it, each one checkable.
  - explanation: A short reason for setting it, in the words the user reads.
