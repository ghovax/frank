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

// Bash tool across its lifecycle: running, completed, and failed.

export const BashRunning: Story = {
  args: {
    name: "bash",
    arguments: {
      command: "rg 'def connect' src/",
      read_only: true,
      justification: "Finding every connect() definition before changing its signature",
      risk: "low",
    },
    toolCallId: "call_00_a1B2c3D4e5F6g7H8i9J0k",
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
    toolCallId: "call_00_kL0m9N8oP7qR6sT5uV4wX",
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
    toolCallId: "call_00_fF1aA2bB3cC4dD5eE6fF7g",
    status: "failed",
    result: { code: "tool_error", message: "Command exited with code 1: src/api/server.py:41: type error ..." },
  },
};

// Human-in-the-loop: permission approval flow for a sandboxed tool call.

export const AwaitingPermission: Story = {
  args: {
    name: "bash",
    arguments: {
      command: "cat /etc/hosts",
      read_only: true,
      justification: "Reading the hosts file to debug the DNS redirect",
      risk: "medium",
    },
    toolCallId: "call_00_gG8hH7iI6jJ5kK4lL3mM2n",
    status: "input_required",
    permission: {
      requestId: "perm-ctx_session_abc123",
      justification: "Sandbox approval required: this command reads outside the working directory (/etc/hosts).",
      risk: "medium",
    },
  },
};

// Human-in-the-loop: ask_user question awaiting an answer from the user.

export const AwaitingAnswer: Story = {
  args: {
    name: "ask_user",
    arguments: {
      justification: "The database choice changes the whole implementation, so asking before building.",
    },
    toolCallId: "call_00_nN1oO2pP3qQ4rR5sS6tT7u",
    status: "input_required",
    question: {
      requestId: "q-ctx_session_def456",
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

// Specialized tools (read_file, edit_file, web_search, etc.) in their completed state.

export const ReadFileCompleted: Story = {
  args: {
    name: "read_file",
    arguments: {
      file_path: "/Users/me/proj/src/db/client.py",
      offset: 1,
      limit: 3,
      justification: "Reading the connection module before editing it",
    },
    toolCallId: "call_00_uU8vV9wW0xX1yY2zZ3aA4b",
    status: "completed",
    result: {
      code: "read_completed",
      path: "/Users/me/proj/src/db/client.py",
      start_line: 1,
      end_line: 3,
      total_lines: 42,
      content: "     1\timport psycopg2\n     2\t\n     3\tdef connect(uri):",
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
    toolCallId: "call_00_bB5cC6dD7eE8fF9gG0hH1i",
    status: "completed",
    result: {
      code: "edit_completed",
      path: "/Users/me/proj/src/db/client.py",
      characters: 412,
      replacements: 1,
      before: "import psycopg2\n\ndef connect(uri):\n    return psycopg2.connect(uri)\n",
      after: "import psycopg2\n\ndef connect(uri, timeout=5):\n    return psycopg2.connect(uri, connect_timeout=timeout)\n",
    },
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
    toolCallId: "search_00_iI2jJ3kK4lL5mM6nN7oO8p",
    status: "running",
  },
};

export const WebSearchCompleted: Story = {
  args: {
    name: "web_search",
    arguments: {
      query: "psycopg2 connect timeout parameter",
      justification: "Confirming the timeout API before using it",
      result_count: 5,
    },
    toolCallId: "search_00_pP9qQ0rR1sS2tT3uU4vV5w",
    status: "completed",
    result: {
      code: "web_search_completed",
      query: "psycopg2 connect timeout parameter",
      results: [
        { title: "psycopg2.connect — Psycopg 2.9.10 documentation", url: "https://www.psycopg.org/docs/module.html" },
        { title: "Connection arguments — Psycopg 2.9.10 documentation", url: "https://www.psycopg.org/docs/connection.html" },
      ],
    },
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
    toolCallId: "call_00_wW6xX7yY8zZ9aA0bB1cC2d",
    status: "completed",
    result: { code: "task_completed", artifact: "Auth flow: login() -> issue_session() -> set_cookie(). No token rotation found." },
    agents: [
      { id: "reader", name: "reader", title: "Reader" },
      { id: "researcher", name: "researcher", title: "Researcher" },
      { id: "builder", name: "builder", title: "Builder" },
    ],
  },
};

export const BackgroundBash: Story = {
  args: {
    name: "bash",
    arguments: {
      command: "npm run build",
      read_only: false,
      risk: "medium",
      justification: "Building the project in the background while I prepare the deployment config",
      background: true,
    },
    toolCallId: "call_00_dD3eE4fF5gG6hH7iI8jJ9k",
    status: "running",
    result: { code: "background_task_scheduled", task_id: "bg_00_kK0lL1mM2nN3oO4pP5qQ6r" },
  },
};

export const FindFilesCompleted: Story = {
  args: {
    name: "find_files",
    arguments: {
      pattern: "**/*.config.{ts,js}",
      justification: "Locating all configuration files before the migration",
    },
    toolCallId: "call_00_rR7sS8tT9uU0vV1wW2xX3y",
    status: "completed",
    result: {
      code: "find_completed",
      pattern: "**/*.config.{ts,js}",
      matches: ["/Users/me/proj/tsconfig.json", "/Users/me/proj/web/vite.config.ts", "/Users/me/proj/web/next.config.ts"],
      count: 3,
    },
  },
};

export const SearchContentCompleted: Story = {
  args: {
    name: "search_content",
    arguments: {
      pattern: "function\\s+connect\\s*\\(",
      include: "*.py",
      justification: "Finding all connect() function definitions across the Python codebase",
    },
    toolCallId: "call_00_yY4zZ5aA6bB7cC8dD9eE0f",
    status: "completed",
    result: {
      code: "search_completed",
      pattern: "function\\s+connect\\s*\\(",
      count: 2,
      matches: ["src/db/client.py:14:def connect(uri):", "src/api/server.py:41:def connect(self):"],
    },
  },
};
