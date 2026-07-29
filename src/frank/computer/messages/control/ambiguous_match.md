Your query "{{ query }}" matched several elements that look the same — same name, role, and surrounding context — so I can't tell which one you mean and did not act. The closest matches:

{{ candidates }}

Narrow it: add a discriminator to find_one (clickable=True for something you can act on, clickable=False for plain text, or name=/context= copied exactly from one of the candidates above), or describe the target more specifically — its exact label or the section it sits under. Don't act on a plain find_many result by position for a state change; use find_one so an unclear target is caught instead of guessed.
