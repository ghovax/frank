Send a message to another session: one you created, or the one that created you.

**Upward, this is how you report back.** A message to the session that created you is your deliverable, and it is the only thing that reaches whoever waits. They cannot see your transcript, your tool calls or your reasoning. So the message must stand on its own: what you found, the evidence for it, what you changed, and whatever is still uncertain.

To send it does not end your turn and does not stop your work. Send it when you hold the answer, then finish as you normally would.

**Downward, this is how you brief a peer and follow up with it.** `create_session` makes a peer and stops there. **The message you send next is what sets it working**, so it carries the whole brief: the goal, the paths, the constraints, and the shape of the answer you want back. A peer cannot see your conversation, so whatever you leave out is gone.

Afterwards the same tool corrects, narrows, or adds to what you asked. A message to a session that *already works* is steered into its current turn at the next safe point, instead of queued behind it. So you can redirect a peer in the middle of its task, without a wait. A message to an idle session simply starts its next turn.

This call returns as soon as the harness accepts the message. There is nothing to wait for and nothing to poll. A reply reaches you on its own, as a new message.
