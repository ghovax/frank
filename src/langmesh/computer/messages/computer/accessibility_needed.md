Accessibility is not granted to the process running LangMesh, so it cannot read or control other applications.

**Do not guess which entry to enable.** macOS attaches this permission to the application that *launched* LangMesh, not to LangMesh itself: started from a terminal it is that terminal (Terminal, iTerm), and started from the packaged app it is LangMesh. Tell the user that, ask which they used, and let them find the right row in System Settings › Privacy & Security › Accessibility. An entry named "LangMesh" may not exist at all.

Say this plainly and wait for them. Nothing on the screen is reachable until it is granted, and no other approach gets around it.
