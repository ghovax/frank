{{ system_prompt }}

{{ context }}

## Background tasks

After spawning sub-agents or background tasks, do not make busy-work tool calls (sleep, echo, ps) to check on them. Simply stop making tool calls. The harness automatically injects background results when they complete and resumes the conversation.

{{ tasks_section }}

{{ task_instruction }}