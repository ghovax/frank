Create a peer session and give it work.

A peer is a separate process running its own agent profile, with its own context window. Use one when the work is genuinely separable — parallel investigations, a broad search across a subsystem you are not in, review or test discovery while you implement. Do not use one for a small edit, for work that needs the context you are already holding, or for a judgment call: a peer gathers evidence, you decide.

This returns as soon as the peer exists, with its id. It does not wait for the work. **The peer sends you its answer as a message when it is done**, which arrives on its own and wakes you if your turn has ended — so start the work, carry on with whatever does not depend on it, and end your turn when everything left does. Never poll a peer to find out whether it has finished.

The peer is a child of yours: it is reaped when you end, and it cannot hold authority you do not have — a permission mode looser than yours is clamped down to yours. Pass `read_only` for a peer that only needs to investigate. Being reaped when you end is a backstop, not a plan: nothing ends a peer when its work finishes, so `end_session` it yourself once you have the answer and do not expect to follow up.

It cannot see your conversation, so `message` must be self-contained: the goal, the paths, the constraints, and the shape of the answer you want back.
