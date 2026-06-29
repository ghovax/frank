import type { Meta, StoryObj } from "@storybook/react";
import { fn } from "@storybook/test";
import { ToolCall } from "./tool-call";

const meta = {
  title: "Components/ToolCall",
  component: ToolCall,
  parameters: { layout: "padded" },
  args: {
    onPermission: fn(),
    onQuestion: fn(),
  },
} satisfies Meta<typeof ToolCall>;

export default meta;
type Story = StoryObj<typeof meta>;

// --- bash across its lifecycle ---

export const BashRunning: Story = {
  args: {
    name: "bash",
    arguments: {
      command: "rg 'def connect' src/",
      read_only: true,
      justification: "Finding every connect() definition before changing its signature",
      risk: "low",
    },
    sequenceNumber: 1,
    toolCallId: "call_bash_running",
    status: "running",
  },
};

export const BashCompleted: Story = {
  args: {
    name: "bash",
    arguments: {
      command: "rg 'def connect' src/",
      read_only: true,
      justification: "Finding every connect() definition",
      risk: "low",
    },
    sequenceNumber: 2,
    toolCallId: "call_bash_done",
    status: "completed",
    result: {
      code: "bash_completed",
      output: "src/db/client.py:14:def connect(uri):\nsrc/api/server.py:41:def connect(self):",
      pid: 48213,
      size: 78,
    },
  },
};

export const BashFailed: Story = {
  args: {
    name: "bash",
    arguments: {
      command: "npm run typecheck",
      read_only: true,
      justification: "Verifying the auth fix did not regress types",
      risk: "low",
    },
    sequenceNumber: 3,
    toolCallId: "call_bash_fail",
    status: "failed",
    result: { code: "tool_error", message: "Command exited with code 1: src/api/server.py:41: type error ..." },
  },
};

// --- human-in-the-loop: permission approval ---

export const AwaitingPermission: Story = {
  args: {
    name: "bash",
    arguments: {
      command: "cat /etc/hosts",
      read_only: true,
      justification: "Reading the hosts file to debug the DNS redirect",
      risk: "medium",
    },
    sequenceNumber: 4,
    toolCallId: "call_perm",
    status: "input_required",
    permission: {
      requestId: "perm-session-1",
      justification: "Sandbox approval required: this command reads outside the working directory (/etc/hosts).",
      risk: "medium",
    },
  },
};

// --- human-in-the-loop: ask_user question ---

export const AwaitingAnswer: Story = {
  args: {
    name: "ask_user",
    arguments: {
      justification: "The database choice changes the whole implementation, so asking before building.",
    },
    sequenceNumber: 5,
    toolCallId: "call_question",
    status: "input_required",
    question: {
      requestId: "q-session-1",
      questions: [
        {
          question: "Which database should the new service use?",
          header: "Database",
          options: [
            { label: "Postgres (Recommended)", description: "Relational, well-supported, fits the access patterns." },
            { label: "SQLite", description: "Zero-config, file-based — fine for a single node." },
            { label: "MongoDB", description: "Document store — only if the data is genuinely document-shaped." },
          ],
        },
        {
          question: "Which regions should we deploy to?",
          header: "Regions",
          multiple: true,
          options: [
            { label: "us-east-1" },
            { label: "eu-west-1" },
            { label: "ap-southeast-2" },
          ],
        },
      ],
    },
  },
};

// --- the new specialized tools, completed ---

export const ReadFileCompleted: Story = {
  args: {
    name: "read_file",
    arguments: {
      file_path: "/Users/me/proj/src/db/client.py",
      justification: "Reading the connection module before editing it",
    },
    sequenceNumber: 6,
    toolCallId: "call_read",
    status: "completed",
    result: {
      code: "read_completed",
      path: "/Users/me/proj/src/db/client.py",
      start_line: 1,
      end_line: 3,
      total_lines: 42,
      content: "1: import psycopg2\n2: \n3: def connect(uri):",
    },
  },
};

export const EditFileCompleted: Story = {
  args: {
    name: "edit_file",
    arguments: {
      file_path: "/Users/me/proj/src/db/client.py",
      old_string: "def connect(uri):",
      new_string: "def connect(uri, timeout=5):",
      justification: "Adding a timeout parameter to connect()",
      risk: "low",
    },
    sequenceNumber: 7,
    toolCallId: "call_edit",
    status: "completed",
    result: { code: "edit_completed", path: "/Users/me/proj/src/db/client.py", created: false, characters: 412 },
  },
};

export const WebSearchRunning: Story = {
  args: {
    name: "web_search",
    arguments: {
      query: "psycopg2 connect timeout parameter",
      justification: "Confirming the timeout API before using it",
      result_count: 5,
    },
    sequenceNumber: 8,
    toolCallId: "call_search",
    status: "running",
  },
};

export const SpawnAgentCompleted: Story = {
  args: {
    name: "spawn_agent",
    arguments: {
      prompt: "Map the auth flow across src/api and report back the call graph and any session-handling code.",
      agent: "reader",
      read_only: true,
      justification: "Mapping the auth flow in parallel while I implement the DB layer",
    },
    sequenceNumber: 9,
    toolCallId: "call_spawn",
    status: "completed",
    result: { code: "task_completed", artifact: "Auth flow: login() -> issue_session() -> set_cookie(). No token rotation found." },
    agents: [
      { id: "reader", name: "reader", title: "Reader" },
      { id: "researcher", name: "researcher", title: "Researcher" },
      { id: "builder", name: "builder", title: "Builder" },
    ],
  },
};
