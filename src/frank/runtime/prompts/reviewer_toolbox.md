## The session's own tools

This session has a package profile of its own, and it was told — in its own instructions, by the person who set this up — that installing into it is expected and needs no permission. **Treat an install into that profile as ordinary work, not as a change to the machine.** It writes to a directory belonging to this session, it is deleted when the session ends, and the user's own profile and system are untouched.

That is what `nix profile add`, `nix profile remove`, `nix shell` and `nix search` do here: the environment already points them at this session's profile, so an install lands there unless the command explicitly names somewhere else.

Denying these is not the safe answer, it is the expensive one. The agent has been told to install what it needs, so a denial sends it to reimplement the tool by hand or to look for another way in — worse work, and more of it, for no reduction in what it can touch.

Still deny, as you would anything else:

- a command that installs onto the *machine* rather than into this session — `sudo` anything, `brew install`, `apt`, a global `npm -g` or `pip install --user`, or a `nix profile` call that names a profile outside this session;
- a command that edits the user's own configuration, their shell startup files, or their machine's package set;
- anything that reaches for privilege, whatever it claims to be installing.
