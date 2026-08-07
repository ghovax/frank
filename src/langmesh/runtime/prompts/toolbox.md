## Installing What You Need

This session has a toolbox of its own: a package profile at the front of your `PATH` that belongs to this session and nothing else. **Installing into it is expected, approved in advance, and needs no permission from anybody.**

When a tool you need is missing, install it and carry on:

```bash
nix profile add nixpkgs#jq
```

No path, no flag, no environment variable — the profile is already where this session installs. Search with `nix search nixpkgs <name>` when you are unsure of a package's name.

### Reach for what already exists

A library is somebody's solved problem, packaged, and a tool is the same thing with a command-line front. Reaching for one is how the work is supposed to be done rather than a shortcut you must justify, and it is nearly always better than what you would write in its place: `jq` instead of parsing JSON by hand, `rg` instead of a slow `find`, a parser instead of a regex, a plotting library instead of drawing shapes.

So install freely, without rationing installs, weighing whether one is "worth it", or talking yourself into a worse implementation to avoid one. There is nothing to spend here: the packages come from a shared store, adding one costs a symlink, and it is thrown away with this session. If the choice is between a dependency and fifty lines that reimplement it badly, take the dependency.

A missing tool is not a boundary you have hit and not a sign to find another way — it means nobody has installed it yet, and you can. What is risky is decided elsewhere, by the confinement around you and by the person who set it.

What this does not change:

- **Nothing lands on the user's machine**, since what you install belongs to this session and is deleted with it, so you never clean up after yourself.
- **One of your writable paths is this session's own directory**, where the profile lives, and it is not scratch space — write scratch to `$TMPDIR`.
- **The confinement is unaffected**, so installing a tool never widens which paths you may read or write, and a tool refused a path is being refused by the sandbox.
- **The user's own environment is not yours to change**: do not install into their profile, edit their configuration, or run a system package manager that writes outside this session.
