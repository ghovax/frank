You extract inline **citation marker numbers** from a single passage of an academic paper.

The passage may contain HTML. Return **every numeric citation marker the passage actually cites** — bracketed markers like `[12]`, superscripts, and comma/range lists. **Expand ranges**: `[1-3]` becomes `1`, `2`, `3`. Include **only** markers cited in this passage; do **not** invent numbers, and do **not** include figure, table, equation, or section numbers.

Return them as `marker_numbers` (a list of integers) — an **empty list** if the passage cites nothing.
