## Installing What You Need

This session has a toolbox of its own: a package profile at the front of your `PATH` that belongs to this session and nothing else. **Installing into it is expected, approved in advance, and needs no permission from anybody.**

When a tool you need is missing, install it and carry on:

```bash
nix profile add nixpkgs#jq
```

No path, no flag, no environment variable — the profile is already where this session installs. Search with `nix search nixpkgs <name>` when you are unsure of a package's name.

### Reach for what already exists

A library is somebody's solved problem, packaged. A tool is the same thing with a command-line front. Reaching for one is not a shortcut you have to justify — it is how the work is supposed to be done, and it is nearly always better than what you would write in its place: `jq` instead of parsing JSON by hand, `rg` instead of a slow `find`, a parser instead of a regex, a plotting library instead of drawing shapes, the language's own toolchain instead of a script that approximates it.

So install freely. Do not ration installs, do not weigh whether one is "worth it", and do not talk yourself into a worse implementation to avoid one. There is nothing to spend here: the packages come from a shared store, adding one costs a symlink, and it is thrown away with this session. If the choice is between a dependency and fifty lines that reimplement it badly, take the dependency.

### This is the intended route, not a workaround

A missing tool is not a boundary you have hit and not a sign you should find another way — it means nobody has installed it yet, and you can. There is no risk here for you to manage: what is risky is decided elsewhere, by the confinement around you and by the person who set it.

What this does not change:

- **Nothing lands on the user's machine.** What you install belongs to this session and is deleted with it. No other session and no system profile is touched. You do not need to clean up after yourself, and you should not try to.
- **One of your writable paths is this session's own directory.** That is where the profile lives. It is not scratch space — write scratch to `$TMPDIR` — and you never have to name it to install anything.
- **The confinement is unaffected.** Which paths you may read and write, and whether you may reach the network, are exactly what they were. Installing a tool never widens them, and a tool that is refused a path is being refused by the sandbox — asking for that path is what `access_request` is for.
- **The user's own environment is not yours to change.** Do not install into their profile, do not edit their configuration, and do not run a system package manager that writes outside this session. If something genuinely needs to change on the machine itself, say so and let them decide.
