Download a file from a URL to a path, defeating typical bot/TLS blocks.

Uses full browser TLS/HTTP2 fingerprint impersonation (and the configured proxy), so files that a plain download gets blocked from still come through. For reading a page's text, use fetch_url instead — this saves raw bytes (PDFs, archives, data). It cannot pass an interactive JavaScript challenge or CAPTCHA. This tool writes a file and is unavailable to read-only agents.

Sync-if-fast: it waits up to ``timeout`` seconds for the download inline; one still running past ``timeout`` moves to the background and completes on its own (the destination path is held against concurrent edits until it finishes). ``timeout`` is that inline-wait window (the same meaning as bash's ``timeout``); ``hard_deadline`` is the separate network cutoff that aborts the transfer; ``background=true`` backgrounds immediately.

Arguments:
  - url: Fully-formed http(s) URL of the file to download.
  - path: Destination path (relative to the working directory, or absolute).
  - location: The project location to save into — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
  - timeout: Inline-wait window in seconds before the download backgrounds (does not abort it).
  - hard_deadline: Network deadline in seconds that aborts the transfer itself.
  - background: Skip the inline wait and background the download immediately.
  - explanation: A concise, user-facing reason for this download.
