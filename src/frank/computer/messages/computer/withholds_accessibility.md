{{ app }} is showing a window {{ width }}×{{ height }} points in size and publishes nothing about it to macOS accessibility, so there are no controls here to read or act on. The window server can see it; the application declines to describe it.

This is the application's own choice, and the one handshake that sometimes changes it — `AXManualAccessibility` — has already been sent. The other, `AXEnhancedUserInterface`, is deliberately never sent, because it makes some applications move their windows out from under the person using them.

Tell the user which window it is and ask them to do the step themselves. Don't act blind.
