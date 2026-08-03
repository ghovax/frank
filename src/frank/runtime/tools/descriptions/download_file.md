Download a file from a URL to a path, past the usual bot and TLS blocks.

It impersonates a full browser TLS and HTTP/2 fingerprint, and it uses the configured proxy, so a file that a plain download cannot reach still comes through. Use `fetch_url` to read a page's text; this tool saves raw bytes, such as a PDF, an archive or a dataset. It cannot pass an interactive JavaScript challenge or a CAPTCHA. This tool writes a file, and a read-only agent does not have it.

It waits up to `timeout` seconds for the download. A download still running after `timeout` moves to the background and finishes on its own, and the harness holds the destination path against a concurrent edit until it does. `hard_deadline` is a separate network cutoff, and it aborts the transfer itself. Set `background=true` to background it at once.

Arguments:
  - url: A complete http or https URL for the file.
  - path: Where to save it, relative to the working directory or absolute.
  - location: Which workspace location receives the file — its URI or its name, from the locations in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
  - timeout: How many seconds to wait inline before the download moves to the background. It does not abort the download.
  - hard_deadline: How many seconds before the transfer itself aborts.
  - background: Skip the inline wait, and background the download at once.
  - explanation: A short reason for the download, in the words the user reads.
