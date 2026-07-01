**Open a live preview** in the side preview panel — a sandboxed iframe pointed at a URL or a local file.

Use this for visual output, interactive artifacts, generated HTML, diagrams, charts, maps, or pages the user should inspect while keeping the chat transcript readable.

Parameters:
- `url` (`str`, required): An `http(s)` URL, absolute local file path, or path relative to the working directory.
- `title` (`str`, optional): Short title shown in the preview panel.
- `height` (`int`, optional): Fixed height in pixels, from 120 to 900. Leave as `0` for automatic sizing when possible.
- `artifact_id` (`str`, optional): Stable preview id. Omit for a new preview.
- `artifact_update_mode` (`str`, optional): `append`, `replace`, `update`, or `upsert`.
- `artifact_target_id` (`str`, optional): Existing preview id to refresh.
- `summary` (`str`, optional): One-line description of what the preview shows.

For generated visuals, write a complete HTML file first, then preview that file. Prefer proven libraries for charts, diagrams, maps, and other visual output.
