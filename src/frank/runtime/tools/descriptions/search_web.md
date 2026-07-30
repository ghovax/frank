Search the web using Exa. Returns a ranked list of results with titles, URLs, and a summary of each — so you can often answer directly without fetching the page.

Most searches finish quickly and return their ``web_search_completed`` results directly from this call. A slow search returns a ``web_search_started`` acknowledgement instead; its results are then delivered to you automatically as a separate ``web_search_completed`` message carrying the same ``job_id`` — never call ``read_turn`` on the identifier and never poll for it. Just keep working (you can start several searches at once); pending results appear on their own.

Use this when you need current information from the internet, recent events, changing documentation, standards, prices, schedules, or external knowledge not available in the training data. Use ``fetch_url`` when the URL is already known instead of searching for it.

Arguments:
  - query: The search query.
  - explanation: A concise, user-facing description of why this search is needed.
  - result_count: Number of results to return (1-10, default 5).
