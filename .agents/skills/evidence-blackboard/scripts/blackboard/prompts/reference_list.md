You split an academic paper's **reference/bibliography section** into individual numbered entries.

The input is a **JSON array of text fragments** from the reference section, in order. Concatenate them, then split the result into entries — a single entry may span **several fragments**, and one fragment may hold **several entries**.

Keep each entry's text **fully verbatim** — authors, title, venue, year, everything. Do **not** summarize, shorten, or normalize it. The `marker_number` is the entry's own number in the list (`1`, `2`, `3`, …); if the list is unnumbered, number the entries in the order they appear.

Return the list of `references`; for each, give its `marker_number` and its full verbatim text as `raw_string`. Return an **empty list** if the text holds no references.
