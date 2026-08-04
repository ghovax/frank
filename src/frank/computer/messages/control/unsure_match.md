Your query "{{ query }}" had no clear winner — the best match scored barely ahead of the next, so choosing one would have been a guess, and nothing was acted on. The closest matches:

{{ candidates }}

**The element you want is almost certainly in that list** — pick from it rather than searching again. Rewording is the wrong move here and usually costs two more calls: the ranking is this close because the words available cannot separate these elements, so different words will not separate them either.

Choose by something other than wording. Each candidate carries its `role`, its `parent` and its `bounds`; controls that read alike are separated by where they sit or by what they hang off, and which of the two answers depends on the application rather than on anything you can guess. Read the candidates, decide which one you meant, and act on that `id` directly.
