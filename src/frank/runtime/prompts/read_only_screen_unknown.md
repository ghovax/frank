a screen script whose effect could not be established by reading it{{ detail }}

This is not a complaint that the script changes state. It may well change nothing. The point is that this session is read-only, so a script may run only where somebody can read beforehand what it will do. This script calls into something that cannot be read from here.

The usual cause is an import. The module is neither a saved workflow nor a skill's own package — it is a third-party library, or a name that does not resolve. Move the part that drives the screen into the script itself, so the primitives it calls are visible there. Keep the computation you need in the standard library.
