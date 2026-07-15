**Read another A2A task** in this context — a sibling or agent task — by its id, returning its current status and artifact (deliverable).

Use this to coordinate with agents working alongside you: check whether a sibling has finished and read what it produced, then build on it. Task ids are the ones returned when an *agent* is spawned.

This is **not** how you retrieve background results. A `web_search` (`search-…`) or background-bash (`bg-…`) identifier is **not** a readable task — those results are delivered to you **automatically** when ready. **Never** dispatch `read_task` on them and **never** use it to poll.
