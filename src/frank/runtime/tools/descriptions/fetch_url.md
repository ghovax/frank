Fetch the content at a URL, and convert it to the format you ask for.

Use this for a URL you already know. Use `search_web` to find one. It returns the page text, and it handles both JavaScript-rendered pages and common anti-bot walls, through rendering fallbacks. A very large response is truncated inline and carries an `output_file` with the full conversion. Use `download_file` for a raw binary file. This tool only reads.

It waits up to `timeout` seconds and returns the content directly. A fetch still running after `timeout` moves to the background, and its result reaches you when it lands, so a slow page never blocks your turn. Raise `timeout` to wait longer, or set `background=true` to background it at once. `hard_deadline` is a separate network cutoff, and it aborts the request itself.

Arguments:
  - url: A complete http or https URL. It is fetched exactly as you give it. Nothing rewrites the scheme, so write https yourself where you mean https.
  - format: "markdown" (the default), "text", or "html".
  - timeout: How many seconds to wait inline before the fetch moves to the background. It does not abort the fetch.
  - hard_deadline: How many seconds before the network request itself aborts.
  - background: Skip the inline wait, and background the fetch at once.
  - explanation: A short reason for the fetch, in the words the user reads.
