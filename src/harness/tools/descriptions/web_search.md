**Search the web** (via Exa). Returns results with titles, URLs, and summaries.

The search runs in the **background**. You do **not** fetch the results yourself: when it finishes, the results are **delivered automatically** as a separate `web_search_completed` message carrying the same `task_identifier`. This call only returns a `web_search_started` acknowledgement.

- **Never** call `read_task` on the returned identifier, and **never poll** for it — just keep working. You can start several searches at once; their results arrive on their own. If everything left to do depends on the results, simply finish your turn: the harness starts a fresh turn on its own and re-engages you the moment they land, so nothing is lost by ending now.
- Use this when you need **current** information: recent events, live documentation, standards, prices, schedules, or anything likely to have changed.
- For retrieving a **specific known URL**, prefer **fetch_url** instead.

Always provide a concise **justification** — one open-ended clause that reads as running words, never a `label: detail` heading.
