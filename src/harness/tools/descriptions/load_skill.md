**Load a specialized skill** when the task matches one of the skills listed in your system prompt.

Injects the skill's instructions and resources into the conversation. The output contains detailed workflow guidance as well as references to scripts, files, and other resources in the skill's directory.

- `name` must match a skill listed in the **Available skills** section of your system prompt.
- Loading a skill is how you follow project- or domain-specific workflows — **do not guess them.** When a task matches a skill's title or description, load that skill *before* acting.

Always provide a concise *justification* that states why this skill is relevant to the task.
