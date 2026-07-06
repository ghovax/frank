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
  return { id: `msg_${crypto.randomUUID().slice(0, 8)}`, timestamp: new Date().toISOString(), ...partial };
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
          content: "read_file",
          meta: {
            toolCallId: "call_00_a1B2c3D4e5F6g7H8i9J0k",
            status: "completed",
            arguments: { file_path: "/Users/me/proj/src/api/health.py", justification: "Reading the existing health endpoint before adding a DB check" },
            result: { code: "read_completed", path: "/Users/me/proj/src/api/health.py", start_line: 1, end_line: 30, total_lines: 64, content: "     1\tfrom fastapi import APIRouter\n     2\t\n     3\trouter = APIRouter()\n     4\t\n     5\t@router.get(\"/healthz\")\n     6\tasync def healthz():" },
          },
        })}
      />
      <ChatMessageItem
        {...sharedProps}
        message={message({
          role: "tool_call",
          content: "edit_file",
          meta: {
            toolCallId: "call_00_kL0m9N8oP7qR6sT5uV4wX",
            status: "completed",
            arguments: {
              file_path: "/Users/me/proj/src/api/health.py",
              old_string: "from fastapi import APIRouter",
              new_string: "from fastapi import APIRouter\n\nfrom src.db.pool import pool",
              justification: "Importing the shared database pool",
              risk: "low",
            },
            result: { code: "edit_completed", path: "/Users/me/proj/src/api/health.py", characters: 124, replacements: 1 },
          },
        })}
      />
      <ChatMessageItem
        {...sharedProps}
        message={message({
          role: "tool_call",
          content: "bash",
          meta: {
            toolCallId: "call_00_fF1aA2bB3cC4dD5eE6fF7g",
            status: "completed",
            arguments: { command: "uv run python -m pytest tests/api/ -x -q", read_only: true, justification: "Verifying the new endpoint did not regress the API tests", risk: "low" },
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
            toolCallId: "call_00_nN1oO2pP3qQ4rR5sS6tT7u",
            status: "input_required",
            arguments: { justification: "The retry policy changes behavior, so confirming before shipping." },
            question: {
              requestId: "q-ctx_session_def456",
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
