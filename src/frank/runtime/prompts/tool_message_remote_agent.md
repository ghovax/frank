Hand a message to an agent on another host, and return its reply.

The exchange is one-shot. The agent keeps no history between messages, so each message stands alone. It cannot read this filesystem, so send the content the task needs instead of a path — and send only that content, because it leaves this machine.
