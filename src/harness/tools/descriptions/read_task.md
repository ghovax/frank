**Read another A2A task** in this context — a sibling or agent task — by its id, returning its current status and artifact (deliverable).

Use this to coordinate with externally supplied sibling A2A task ids: check whether a sibling has finished and read what it produced, then build on it.

This is **not** how you retrieve background results. A `web_search` (`search-…`), background-bash (`bg-…`), or spawned-agent (`agent-…`) handle is **not** a readable task — those results are delivered to you **automatically** when ready. Use `cancel_agent` only when a spawned agent should be stopped. **Never** dispatch `read_task` on background handles and **never** use it to poll.
