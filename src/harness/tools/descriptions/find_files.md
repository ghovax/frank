Find files by name across a location, matching a path glob. Results come back newest-first by modification time and honor the location's `.gitignore`, so build output, dependencies, and anything else the project excludes stay out of the results.

The glob follows the usual shell conventions — `**` spans directories, while `*` and `?` stay within a single path segment. Batch several lookups in one response when each answers something you need. For an open-ended hunt that will take several rounds of reasoning, hand it to `spawn_agent` instead.

Leave `include_ignored` off by default — that focus on the project's real source is what makes the results useful. Turn it on only when the file you actually need is one the project ignores (a build artifact, a generated file, something under a gitignored directory) and you have a specific reason to reach it.

This tool is **read-only**.
