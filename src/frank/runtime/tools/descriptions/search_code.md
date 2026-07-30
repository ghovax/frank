Search the codebase by meaning, in plain language.

Ranks the project's code against a natural-language query (semantic similarity plus lexical overlap) and returns just the best-matching chunks with their file and line range — a fraction of the tokens of grepping and reading whole files — finding code by what it does, not its exact name. Use ``bash`` with ripgrep for an exact string or filename; use this to find code by meaning. This tool is read-only.

Arguments:
  - query: What you are looking for, in plain language.
  - top_k: How many matching chunks to return (default 10).
  - reindex: Rebuild the code index first — pass this after you have edited files and need fresh results.
  - explanation: A concise, user-facing reason for this search.
