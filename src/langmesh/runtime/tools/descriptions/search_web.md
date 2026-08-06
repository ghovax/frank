Search the web with Exa. It returns a ranked list of results, each with a title, a URL and a summary, so you can often answer without fetching the page.

Most searches finish quickly and return their `web_search_completed` results from this call. A slow search returns a `web_search_started` acknowledgement instead. Its results then reach you on their own, as a separate `web_search_completed` message that carries the same `job_id`. Never call `read_turn` on that identifier, and never poll for it. Carry on working — you can start several searches at once, and a pending result appears by itself.

Use this where you need current information: recent events, documentation that changes, standards, prices, schedules, or knowledge outside the training data. Use `fetch_url` where you already know the URL.

Arguments:
  - query: The search query.
  - explanation: A short reason for the search, in the words the user reads.
  - result_count: How many results to return, from 1 to 10. Defaults to 5.
