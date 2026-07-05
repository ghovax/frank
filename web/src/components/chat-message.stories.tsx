import type { Meta, StoryObj } from "@storybook/react";
import { fn } from "@storybook/test";
import { VStack } from "@chakra-ui/react";
import { ChatMessageItem } from "./chat-message";
import type { ChatMessage } from "@/lib/use-chat";

const meta = {
  title: "Components/ChatMessage",
  component: ChatMessageItem,
  parameters: { layout: "padded" },
  args: {
    agents: [{ id: "reader", name: "reader" }],
  },
} satisfies Meta<typeof ChatMessageItem>;

export default meta;
type Story = StoryObj<typeof meta>;

function message(partial: Partial<ChatMessage> & Pick<ChatMessage, "role" | "content">): ChatMessage {
  return { id: Math.random().toString(36).slice(2), timestamp: new Date().toISOString(), ...partial };
}

// Props shared by every message in the Conversation story (the render does not
// take args, so the handlers/agents are passed explicitly here).
const sharedProps = {
  agents: [{ id: "reader", name: "reader" }],
  onPermission: fn(),
  onQuestion: fn(),
};

export const UserMessage: Story = {
  args: { message: message({ role: "user", content: "Add a Postgres connection check to the health endpoint." }) },
};

export const AssistantMessage: Story = {
  args: {
    message: message({
      role: "assistant",
      content: "Added a `GET /healthz/db` endpoint that issues a `SELECT 1` against Postgres and returns 503 on failure. Verified with `curl localhost:8000/healthz/db`.",
    }),
  },
};

export const ErrorMessage: Story = {
  args: {
    message: message({ role: "error", content: "Command exited with code 1: src/api/server.py:41: type error." }),
    onRetry: fn(),
  },
};

export const ThinkingDone: Story = {
  args: {
    message: message({
      role: "thinking",
      content: "The health endpoint already exists at /healthz. I'll add a /healthz/db companion that checks the DB connection with SELECT 1.",
      meta: { status: "done", durationMs: 4200 },
    }),
  },
};

// The full "together" view: a user request, reasoning, a tool run, a follow-up
// read, an ask_user prompt mid-task, and the final assistant answer — laid out
// exactly as they appear in the chat lane.
export const Conversation = {
  render: () => (
    <VStack align="stretch" gap={2}>
      <ChatMessageItem {...sharedProps} message={message({ role: "user", content: "Add a Postgres connection check to the health endpoint." })} />
      <ChatMessageItem
        {...sharedProps}
        message={message({
          role: "thinking",
          content: "The health endpoint already exists at /healthz. I'll add a /healthz/db companion that checks the DB connection.",
          meta: { status: "done", durationMs: 4200 },
        })}
      />
      <ChatMessageItem
        {...sharedProps}
        message={message({
          role: "tool_call",
          content: "grep",
          meta: {
            toolCallId: "s1",
            status: "completed",
            arguments: { pattern: "def healthz", include: "*.py", justification: "Finding the existing health endpoint" },
            result: { code: "search_completed", pattern: "def healthz", count: 1, matches: ["src/api/health.py:8:def healthz():"] },
          },
        })}
      />
      <ChatMessageItem
        {...sharedProps}
        message={message({
          role: "tool_call",
          content: "bash",
          meta: {
            toolCallId: "s2",
            status: "completed",
            arguments: { command: "uv run python -m pytest tests/api/", read_only: false, justification: "Verifying the new endpoint did not regress the API tests", risk: "low" },
            result: { code: "bash_completed", output: "4 passed in 1.32s", pid: 9012, size: 24 },
          },
        })}
      />
      <ChatMessageItem
        {...sharedProps}
        message={message({
          role: "tool_call",
          content: "ask_user",
          meta: {
            toolCallId: "s3",
            status: "input_required",
            arguments: { justification: "The retry policy changes behavior, so confirming before shipping." },
            question: {
              requestId: "q-story",
              questions: [
                {
                  question: "How should the health check handle a transient DB failure?",
                  header: "Failure policy",
                  options: [
                    { label: "Fail fast (Recommended)", description: "Return 503 immediately — the orchestrator handles restarts." },
                    { label: "Retry once", description: "Retry the SELECT 1 once after 200ms before reporting unhealthy." },
                  ],
                },
              ],
            },
          },
        })}
      />
      <ChatMessageItem
        {...sharedProps}
        message={message({
          role: "assistant",
          content: "Added `GET /healthz/db` (returns 200 on `SELECT 1` success, 503 otherwise). All 4 API tests pass.",
        })}
      />
    </VStack>
  ),
};
