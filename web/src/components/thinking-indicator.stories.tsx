import type { Meta, StoryObj } from "@storybook/react";
import { ThinkingIndicator } from "./thinking-indicator";

const meta = {
  title: "Components/ThinkingIndicator",
  component: ThinkingIndicator,
  parameters: { layout: "padded" },
} satisfies Meta<typeof ThinkingIndicator>;

export default meta;
type Story = StoryObj<typeof meta>;

export const ThinkingNow: Story = {
  args: { content: "", status: "thinking" },
};

export const ThoughtWithDuration: Story = {
  args: {
    content: "The health endpoint already exists at /healthz. I'll add a /healthz/db companion that issues a SELECT 1 against Postgres and returns 503 on failure.",
    status: "done",
    durationMs: 7400,
  },
};

export const ThoughtOverAMinute: Story = {
  args: {
    content: "Reasoning about the retry policy tradeoffs before asking the user.",
    status: "done",
    durationMs: 83000,
  },
};
