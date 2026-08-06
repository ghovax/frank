`screen.{{ primitive }}()` needs a live session to drive.

Screen control is reached through the `control_screen` tool, which is where the permission to act on somebody's windows is asked for and granted. Importing this module in a program run any other way — from a shell, from a test — gives you the object but not the authority, so every call raises this.

To exercise a workflow module, call it from a `control_screen` script: the `screen` it imports there is already bound to the target that call named.
