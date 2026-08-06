`{{ primitive }}` is not available to this session, so that call did nothing and nothing before it was affected. It is refused here, at the surface, rather than by reading your script — there is no way to phrase it that would go through.

What you can call is: {{ available }}.

Two things put a name outside that list. It may not be a primitive at all — a place answers only what its own surface implements, and the vocabulary in your context is the authority on which. Or this session's permission mode may narrow what it may run: a read-only session is offered the reading half and not the acting half. If the task genuinely needs something absent from the list above, say so in your answer and let the user decide; do not look for another route to it.
