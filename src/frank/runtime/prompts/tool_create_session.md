Create a peer session.

The peer starts idle. **Nothing runs until you send it a brief with `message_session`.** A peer that you create and never brief is a process that does nothing. So do both, in that order, unless you have a reason to wait.

A peer is a separate process. It runs its own agent profile and holds its own context window, initialized with the conversation you already have. Use one where the work truly separates: parallel investigations, a broad search across a subsystem you are not in, or review while you implement. Do not use one for a small edit or a judgement call. A peer gathers evidence. You decide.

This call returns as soon as the peer exists, and it gives you the peer's id — which is yours to address it with and not a thing to repeat to the user. After you brief it, **the peer sends you its answer as a message when it finishes**. That message arrives on its own, and it wakes you if your turn ended. So start the work, continue with whatever does not depend on it, and end your turn when everything left does depend on it. Never poll a peer to find out whether it finished.

The peer is your child. It is ended when you are, and it can never hold more access than you have: a peer works the way you work, narrowed by whatever its agent profile allows. Choose the peer by its **agent** — that is what decides what it is for and what it may touch.

A peer also works in the same tree you do. Your edits and its edits reach the same files, so tell it which part of the tree is its, and tell it that other agents work beside it.

A peer you create is reaped when you end, and until then it stays. Nothing you can call ends it early — that is the person's to decide — and nothing needs to: a peer whose turn is over holds a record rather than a process, and wakes on the next message.

Arguments:
  - agent: The agent profile the peer runs, from the list this tool enumerates. Required, and never invented.
  - working_directory: Where the peer works. Defaults to yours.
  - explanation: A short reason for creating this peer, in the words the user reads.
