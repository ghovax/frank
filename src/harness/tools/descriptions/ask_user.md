**Ask the user a question** during execution. Use it to:
1. Gather preferences or requirements.
2. Clarify ambiguous instructions.
3. Get a decision on an implementation choice.
4. Offer choices about what direction to take.

**Usage notes:**
- **Only ask when the answer genuinely changes what you would do.** For choices with an obvious default, pick it, say which you picked, and proceed.
- `questions` is a list. Each item has a `question` (full text), a `header` (short label, ~30 chars), and `options` (a list of `{label, description}`). Set `multiple: true` to allow more than one selection.
- When `custom` is enabled (the default), a *"Type your own answer"* option is added automatically — do **not** include an "Other" or catch-all option yourself.
- Answers come back as **arrays of labels**. If you recommend a specific option, make it **first** in the list and add *(Recommended)* to its label.
