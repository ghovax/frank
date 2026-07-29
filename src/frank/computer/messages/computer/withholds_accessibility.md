{{ app }} is displaying a window ({{ width }}, {{ height }}) in size but does not publish it to macOS accessibility, so there are no controls to read or act on. This is the app's own choice and nothing here can change it while it runs: both accessibility handshakes were tried and refused.

It is an Electron application, so it can expose its interface a second way — relaunching it with `--remote-debugging-port` makes it readable through the browser surface instead. That discards whatever is open in it, so ask the user before suggesting it.

Until then, take a screenshot to see the window and ask the user to do the step. Don't act blind.
