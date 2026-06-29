import type { Meta, StoryObj } from "@storybook/react";
import { MarkdownContent } from "./markdown-content";

const meta = {
  title: "Components/MarkdownContent",
  component: MarkdownContent,
  parameters: { layout: "padded" },
} satisfies Meta<typeof MarkdownContent>;

export default meta;
type Story = StoryObj<typeof meta>;

export const RichAnswer: Story = {
  args: {
    content: [
      "Added `GET /healthz/db` — it runs `SELECT 1` and returns **200** on success or **503** on failure.",
      "",
      "Changed files:",
      "- `src/api/health.py` — new `db_health()` handler",
      "- `tests/api/test_health.py` — 2 new cases",
      "",
      "Verification: `uv run pytest tests/api/` → 4 passed.",
      "",
      "Residual risk: a flaky DB under load could trip the check; the orchestrator already restarts on 503, so no retry was added.",
    ].join("\n"),
  },
};

export const CodeBlock: Story = {
  args: {
    content: "The endpoint is a one-liner over the shared pool:\n\n```python\ndef db_health():\n    with pool.connection() as conn:\n        conn.execute(\"SELECT 1\")\n    return {\"db\": \"ok\"}\n```",
  },
};

export const InlineMath: Story = {
  args: {
    content: "The retry budget is bounded by $\\sum_{i=1}^{n} 2^{i-1} \\cdot 100\\text{ms}$, capped at $n=5$.",
  },
};
