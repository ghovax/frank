**Open a live preview** in the side preview panel — a sandboxed iframe pointed at a URL or a local file.

Use this for visual output, interactive artifacts, generated HTML, diagrams, charts, maps, or pages the user should inspect while keeping the chat transcript readable.

**Important: this tool is primarily for previewing existing webpages by URL.** Previewing inline HTML or local files is possible but should be used as a last resort — only when no other tool can produce the output the user needs.

Parameters:
- `url` (`str`, required): An `http(s)` URL, absolute local file path, or path relative to the working directory.
- `title` (`str`, optional): A clear and descriptive title for what the preview shows, formatted as an open-ended sentence fragment (e.g., "Showing the BBC News homepage", "Previewing the generated bar chart from the sales data", "Rendering the Q3 financial report as HTML"). The title must be as descriptive and specific as possible so the user can identify the preview at a glance across multiple open tabs.
- `height` (`int`, optional): Fixed height in pixels, from 120 to 900. Leave as `0` for automatic sizing when possible.
- `artifact_id` (`str`, optional): Stable preview id. Omit for a new preview.
- `artifact_update_mode` (`str`, optional): `append` opens a new preview tab, `replace`/`update` refresh an existing one, `upsert` refreshes if present else appends.
- `artifact_target_id` (`str`, optional): Existing preview id to refresh.
- `summary` (`str`, optional): One-line description of what the preview shows.

For generated visuals, write a complete HTML file first, then preview that file. Prefer proven libraries for charts, diagrams, maps, and other visual output.
