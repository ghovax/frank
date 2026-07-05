The `find` text is not unique in `{{ resolved_path }}` (even after normalizing whitespace): it appears {{ occurrences }} times.

Either:
- Add more surrounding context to the `find` text (include adjacent lines above and below the target), or
- Set `replace_all=true` if you intend to replace every occurrence.

If adding context, re-read the file to get fresh content with accurate line numbers.
