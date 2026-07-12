**Fetches content from a URL.**

- Takes a `url` and an optional `format` (**markdown** by default; also `text` or `html`).
- Fetches the content and converts it to the requested format.
- Use this when you need to retrieve and analyze web content from a **specific URL you already know**.
- For *discovering* information (when you have no URL yet), prefer **web_search** instead.
- The URL must be **fully-formed and valid** (`http` or `https`).
- Handles JavaScript-rendered pages and most anti-bot walls automatically (it renders and falls back across engines) — so it succeeds where a plain download fails.
- Returns page **text**, not binary files. To save a file (PDF, archive, dataset), use **download_file** instead.
- Very large results are **truncated inline**, with the full converted content written to the file named in `output_file` — read that file (with `read_file` offsets) when you need the tail.

This tool is **read-only** and does not modify any files. A short *justification* is welcome when the purpose is not obvious from the URL — one open-ended sentence rather than a `label: detail` title.
