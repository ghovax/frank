a screen script whose effect could not be established by reading it{{ detail }}

This is not a complaint that the script changes state — it may well not. It is that this session is read-only, so a script is only allowed to run when what it will do can be read off it beforehand, and this one calls into something that cannot be read from here.

The usual cause is an import of a module that is not a saved workflow or a skill's own package — a third-party library, or a name that does not resolve. Inline the part that drives the screen, so the primitives it calls are visible in the script itself, and keep the computation you need in the standard library.
