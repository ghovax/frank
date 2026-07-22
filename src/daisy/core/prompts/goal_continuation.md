You attempted to end your turn while an **active goal** is still unresolved:

{{ goal }}

Continue working toward it. Before sending another final answer, resolve the goal with an `update_goal` call:
- If the goal is genuinely satisfied, call `update_goal` with `status="satisfied"`, then give your final answer.
- If the goal is obsolete or the user changed direction, call `update_goal` with `status="cleared"` and explain the change briefly.

Do not send another final answer while the goal is still active. The harness will keep reminding you until the goal is satisfied or cleared.
