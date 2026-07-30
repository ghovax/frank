Fetch content from a URL and convert it to the requested format.

Use this for a specific URL already known; use ``search_web`` to discover one. It returns page text and handles JavaScript-rendered pages and common anti-bot walls through rendering fallbacks. Very large responses are truncated inline and include an ``output_file`` containing the full conversion. Use ``download_file`` for raw binary files. This tool is read-only.

Sync-if-fast: it waits up to ``timeout`` seconds for the fetch inline and returns the content directly; a fetch still running past ``timeout`` moves to the background and its result is injected when it lands, so a slow page never blocks your turn. ``timeout`` is that inline-wait window (the same meaning as bash's ``timeout``) — raise it to wait longer, or set ``background=true`` to background immediately. ``hard_deadline`` is the separate network cutoff that actually aborts the request.

Arguments:
  - url: A fully-formed http or https URL. It is fetched exactly as given — nothing rewrites the scheme, so pass https yourself when you mean https.
  - format: "markdown" (default), "text", or "html".
  - timeout: Inline-wait window in seconds before the fetch backgrounds (does not abort it).
  - hard_deadline: Network deadline in seconds that aborts the request itself.
  - background: Skip the inline wait and background the fetch immediately.
  - explanation: A concise, user-facing reason for this fetch.
