Create a peer session and give it work. Returns what it produced.

A peer is a separate process running its own agent profile, with its own context window. Use one when the work is genuinely separable — parallel investigations, a broad search across a subsystem you are not in, review or test discovery while you implement. Do not use one for a small edit, for work that needs the context you are already holding, or for a judgment call: a peer gathers evidence, you decide.

Most peers finish inside this call and return their deliverable directly. A longer one returns a `peer_session_started` acknowledgement carrying a `task_identifier`; its result is then delivered to you automatically as a separate `peer_session_completed` message. Never poll for it and never call read_task on the identifier — if everything left depends on that peer, end your turn and you will be woken when its result lands.

The peer is a child of yours: it is reaped when you end, and it cannot hold authority you do not have — a permission mode looser than yours is clamped down to yours. Pass `read_only` for a peer that only needs to investigate.

It cannot see your conversation, so `message` must be self-contained: the goal, the paths, the constraints, and the shape of the answer you want back.
