Ask the user one or more questions, and wait for the answers.

Ask only where the answer changes the work. Where a safe default is clear, choose it, say what you chose, and continue.

Where one option is better, say so and put it first. The user should be able to see which one you would pick, and why, without reading every description.

Custom answers are on by default, so never add a redundant "Other" or catch-all option.

An answer comes back as the selected label, a bare string. That includes free text the user typed instead of choosing. Only a question marked `multiple` answers with an array.

Arguments:
  - questions: A list of question objects. Each holds "question" (the full text), "header" (a short label, about 30 characters), "options" (a list of {"label", "description"}), and two optional flags: "multiple" and "custom", which defaults to true.
  - explanation: A short reason for asking, in the words the user reads.
