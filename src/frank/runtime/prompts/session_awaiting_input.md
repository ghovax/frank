That session is waiting on {{ waiting_on }}, and cannot take a message until it is answered. **Your message has not been sent.**

It is working, not stuck. The decision is the user's, and it reaches them through the interface — you cannot answer it, and sending again will not deliver anything. Leave the session alone, and do not create another in its place: a replacement does the same work twice and leaves the first one still waiting.

Read it later with `read_session`, which says what it is waiting on. When the decision is made it carries on by itself, and its answer reaches you as a message.
