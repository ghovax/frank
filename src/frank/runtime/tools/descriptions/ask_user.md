Ask the user one or more questions and receive their answers.

Ask only when the answer genuinely changes the work. If there is a clear safe default, choose it, state the choice, and continue. When recommending an option, place it first and append ``(Recommended)`` to its label. Custom answers are enabled by default, so never add a redundant Other or catch-all option. An answer comes back as the selected label, a bare string — including free text the user typed instead of choosing. Only a question marked `multiple` answers with an array.

Arguments:
  - questions: List of question objects, each with "question" (full text), "header" (short label, max ~30 chars), "options" (list of {"label", "description"}), and optional "multiple" (bool) and "custom" (bool, default true).
  - explanation: A concise, user-facing reason for asking.
