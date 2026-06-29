**Fetches content from a URL.**

- Takes a `url` and an optional `format` (**markdown** by default; also `text` or `html`).
- Fetches the content and converts it to the requested format.
- Use this when you need to retrieve and analyze web content from a **specific URL you already know**.
- For *discovering* information (when you have no URL yet), prefer **web_search** instead.
- The URL must be **fully-formed and valid**; HTTP is upgraded to HTTPS automatically.
- Results are **truncated** if very large — for huge pages, fetch a more specific URL.

This tool is **read-only** and does not modify any files. Always provide a concise *justification* that states why this fetch advances the task.
