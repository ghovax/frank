Send another message to a session you created, and return what it produces.

A message that arrives while the session is mid-turn is injected into that turn rather than queued behind it. This is how you redirect, correct, or ask a follow-up of a peer that is already working — you do not have to wait for it to finish first.

Returns its deliverable directly if the turn is short, or a `peer_session_started` acknowledgement whose result is delivered to you automatically when it lands. Do not poll for it.
