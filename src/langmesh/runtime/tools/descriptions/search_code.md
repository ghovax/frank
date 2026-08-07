Search the codebase by meaning, in plain language.

This ranks the project's code against a natural-language query, by semantic similarity and by word overlap. It returns only the best-matching chunks, each with its file and its line range. That costs a fraction of the tokens that a grep and a set of whole-file reads would cost, and it finds code by what the code does rather than by what somebody named it.

**Never point it at a dense directory** such as `~` or `/Users/<name>`: narrow to the project, to a known subdirectory, or to an exact pattern.

Use `bash` with ripgrep for an exact string or an exact filename. Use this tool to find code by meaning. This tool only reads.

Arguments:
  - query: What you look for, in plain language.
  - top_k: How many matching chunks to return. Defaults to 10.
  - reindex: Build the code index again first. Pass this after you edited files and need fresh results.
  - explanation: A short reason for the search, in the words the user reads.
