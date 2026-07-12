**Open an artifact** in the side artifacts panel — a sandboxed iframe (or image view) pointed at a URL or a local file.

Use this for any sort of visual output, interactive artifacts, images, plots, generated HTML, diagrams, charts, maps, or pages the user should inspect while keeping the chat transcript readable.

The artifact is labelled automatically — the file name for a local file, the URL for a web page — so there is no title to pass.

Parameters:
- `url` (`str`, required): An `http(s)` URL, absolute local file path, or path relative to the working directory.
- `height` (`int`, optional): Fixed height in pixels, from 120 to 900. Leave as `0` for automatic sizing when possible.
- `artifact_id` (`str`, optional): The id returned by a previous `open_artifact` call. Pass it to **update that artifact tab in place** — the panel refreshes the same tab instead of opening a new one, and the new render is kept as a new *version* the user can step through, diff, download, and restore. Omit it for a genuinely new, separate artifact.

**Version history is automatic.** Files you write and artifacts you open are versioned in the background, so an artifact carries its full history. To refresh an artifact you already opened (a regenerated chart, an edited page), pass that same `artifact_id` back. Even if you omit it, writing to the same file path as before is recognized as the same artifact and updates it in place — so you never accidentally spawn duplicate tabs when iterating on one visual. Only use a new path (and no `artifact_id`) when you mean a distinct artifact.

For generated visuals, write a complete HTML file first, then open that file. Prefer proven libraries for charts, diagrams, maps, and other visual output.
