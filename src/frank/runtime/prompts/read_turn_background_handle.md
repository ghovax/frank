The identifier `{{ job_id }}` is a handle for a background *{{ kind }}* job. It is not a turn, and you cannot read it.

Its result reaches you on its own, as a separate completed message. Do not call `read_turn` on it, and do not poll for it. If it has not arrived, carry on. It will appear.

`read_turn` reads a sibling turn that came to you from outside. To look at a peer session, use `read_session`. A peer's answer arrives on its own, as a message.
