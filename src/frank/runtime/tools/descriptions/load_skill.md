Load a specialized skill's instructions into the conversation.

When a task matches a skill listed in ``Available skills``, load that skill before acting rather than guessing its workflow. The result injects the full instructions and references to any scripts, files, or resources it provides.

Arguments:
  - name: The skill name, matching one listed in "Available skills".
  - explanation: A concise, user-facing reason for loading this skill.
