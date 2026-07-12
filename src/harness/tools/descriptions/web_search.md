**Search the web** (via Exa). Returns results with titles, URLs, and summaries.

Most searches finish within seconds and this call returns the **real `web_search_completed` results directly** — read them and continue. Only a slow search returns a `web_search_started` acknowledgement instead; its results are then **delivered automatically** as a separate `web_search_completed` message carrying the same `task_identifier`.

- When you do get a `web_search_started` acknowledgement: **never** call `read_task` on the identifier and **never poll** — just keep working. You can start several searches at once; pending results arrive on their own. If everything left depends on them, simply finish your turn: the harness re-engages you the moment they land.
- Use this when you need **current** information: recent events, live documentation, standards, prices, schedules, or anything likely to have changed.
- For retrieving a **specific known URL**, prefer **fetch_url** instead.

Always provide a concise **justification** — one open-ended clause that reads as running words, never a `label: detail` heading.
