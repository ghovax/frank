What this call needs beyond what the session already holds — a difference against the confinement listed in your context, not a list of everything the call touches.

Omit it entirely when the call works inside paths already writable or readable, which is the usual case. When present it must set `mutates`. Use `writes` and `reads` for paths outside the confinement, and `network` only where the confinement denies the network.

A granted path stays granted for the rest of the session, so ask for the narrowest thing that does the work and never use a path granted for one purpose to do something else. The paths your context lists as refused are refused outright: no request opens one, and asking again in other words is not a different question.
