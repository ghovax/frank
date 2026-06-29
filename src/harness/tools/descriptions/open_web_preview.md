**Open a live web preview** inline in the chat — a sandboxed iframe pointed at a URL or a local file.

Point it at either:
- an `http(s)` URL, or
- a path to a **local file** you have written (absolute, or relative to the working directory).

The preview only references the file or URL — nothing heavy enters your context. A previewed **local HTML file** sizes to its content automatically; an external URL renders as-is (and some sites refuse to load in a frame).

Manage previews with the artifact fields:
- `append` opens a new preview; `replace`/`update` refresh an existing one; `upsert` refreshes if present, else appends.
- Reuse an `artifact_id` with `artifact_update_mode="replace"` to **refresh in place** instead of stacking duplicates.

Do **not** open a preview to make an ordinary text answer feel richer — reach for it only when the deliverable is inherently **visual or interactive**.
