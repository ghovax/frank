Your query "{{ query }}" was anchored to `near="{{ anchor }}"`, but that anchor itself matched nothing clearly — several elements fit those words about equally well, so there is no single place to search near, and nothing was acted on.

An anchor organises the whole ranking around one element. Anchoring to the wrong one is worse than not anchoring at all, because the answer comes back looking just as confident, so this refuses rather than guesses.

Name the anchor by something the surface actually says. Run a plain `find_many` for it first and copy the wording off a result, or anchor to a different neighbour that is unique — a heading, a filename, a label beside the control you want. If nothing near it is unique, drop `near=` and pick from `find_many` by `parent` or `bounds` instead.
