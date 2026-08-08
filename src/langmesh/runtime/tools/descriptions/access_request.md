What this call says about changing anything, and what it needs beyond the confinement listed in your context.

Always include it and set `mutates`: `false` when the call only inspects, or `true` when it changes anything. Add `writes` and `reads` only for paths outside the confinement, and `network` only where the confinement denies the network. A declaration containing only `mutates` asks for no additional access.

A granted path stays granted for the rest of the session, so ask for the narrowest thing that does the work and never use a path granted for one purpose to do something else. The paths your context lists as refused are refused outright: no request opens one, and asking again in other words is not a different question.
