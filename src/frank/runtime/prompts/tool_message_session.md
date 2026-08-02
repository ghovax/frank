Send a message to another session: one you created, or the one that created you.

**Upward, this is how you report back.** A message to the session that created you is the deliverable, and it is the only thing that reaches whoever is waiting: they cannot see your transcript, your tool calls, or your reasoning, so it must stand on its own — what you found, the evidence for it, what you changed, and anything still uncertain.

Sending it does not end your turn and does not stop you working. Send when you have the answer, then finish however you normally would.

Downward, this is how you brief a peer you created and how you follow up with it. `create_session` makes a peer and stops there; **the message you send next is what sets it working**, so it carries the whole brief — the goal, the paths, the constraints, and the shape of the answer you want back. A peer cannot see your conversation, so anything you leave out is gone.

Afterwards the same tool is how you correct, narrow, or add to what you asked. A message to a session that is *already working* is steered into its current turn at the next safe point rather than queued behind it, so you can redirect a peer mid-task without waiting for it to finish. A message to an idle session simply starts its next turn.

Returns as soon as the message is accepted. There is nothing to wait for and nothing to poll: any reply arrives on its own as a new message to you.
