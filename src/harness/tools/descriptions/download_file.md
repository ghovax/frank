**Downloads a file from a URL to disk.**

- Takes a `url` and a destination `path` (relative to the working directory, or absolute).
- Saves the **raw bytes** — use this for PDFs, archives, images, datasets, and any non-text file.
- Uses full browser **TLS/HTTP2 fingerprint impersonation** (and the configured proxy, if any), so files that a plain download gets blocked from still come through.
- Writes to the current location, **local or remote** — the file lands on the location the session is working in.
- For reading a page's *text* as markdown, use **fetch_url** instead.
- It cannot pass a JavaScript "checking your browser" / CAPTCHA challenge; those need a real browser.

This tool **writes a file** and is not available to read-only agents. A short *justification* is welcome when the purpose is not obvious — one open-ended sentence rather than a `label: detail` title.
