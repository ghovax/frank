Search file contents by regular expression across a location, returning each match with its file path and line number. The search honors the location's `.gitignore` — ripgrep walks the tree where available, extended-regex grep otherwise — so it sees only the files the project actually keeps, in a consistent regex dialect on every location.

Use `include` to restrict which filenames are searched and `path` to aim at a subtree or a single file; keep it pointed inside a project rather than sweeping an entire home directory. The results are capped, so don't rely on this for an exact match count — run ripgrep through `bash` when you need a precise number. For an open-ended investigation that spans several rounds, hand it to `spawn_agent` instead.

Leave `include_ignored` off by default — skipping what the project excludes is what keeps the matches signal, not noise from dependencies and build output. Turn it on only when what you are looking for genuinely lives in a gitignored file and you have a specific reason to reach it.

This tool is **read-only**.
