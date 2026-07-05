You are compacting the earlier part of a long working conversation so it fits back into the model's context window without losing what matters. The messages above are the earlier portion of the session; the most recent turns are kept verbatim and are NOT shown to you — your summary replaces everything before them.

Write a dense, faithful **handoff summary** that lets the work continue seamlessly, as if the full history were still present. Capture:

- **Objective and current state** — what the user ultimately wants, and where the work stands right now.
- **Decisions and rationale** — choices already made and why, so they are not relitigated or reversed.
- **Concrete facts discovered** — file paths (`path:line` where known), function/identifier names, config values, command results, root causes, and other evidence that later steps depend on. Preserve exact identifiers; do not paraphrase them away.
- **Changes made so far** — what has been edited, created, run, or verified, and the outcome.
- **Open threads** — what is still pending, blocked, or unresolved, and any commitments made to the user (e.g. a task list still in progress).
- **User preferences and constraints** — anything the user asked for about how the work should be done.

Rules:
- Be comprehensive but not verbose — high semantic density. Every line must carry a fact the continuation needs. Drop pleasantries, restated tool output, and dead ends that no longer matter.
- Preserve exact technical detail (paths, names, numbers, signatures). Losing an identifier is worse than losing prose.
- Do not invent anything not present in the history. If something is uncertain, say so.
- Output only the summary. No preamble, no "Here is the summary", no meta-commentary.
