"use client";

// Design gallery — a backend-less fixture sheet of the transcript's building
// blocks (markdown prose, tool-activity lines and groups, user bubbles, system
// rows, and the panel card shell), all mounted with representative data. It
// exists so layout work can be *measured* rather than eyeballed: drive a real
// browser at /gallery, read bounding boxes off the `data-audit` markers, and
// compare alignment/density across states without wiring up a server. Not
// linked from the app; ConnectionGate exempts this route.

import { Box, Flex, Text, VStack } from "@chakra-ui/react";
import { useState, type ReactNode } from "react";
import type { ChatMessage } from "@/lib/use-chat";
import type { ToolEvent } from "@/lib/tool-event";
import { ChatMessageItem } from "@/components/chat-message";
import { ToolGroup } from "@/components/tool-group";
import { ToolCall } from "@/components/tool-call";
import { MarkdownContent } from "@/components/markdown-content";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolMetaRow } from "@/components/tool-card";
import { useColorMode } from "@/components/ui/color-mode";
import { LuSparkles } from "react-icons/lu";

const now = "2026-01-01T00:00:00Z";

function message(role: ChatMessage["role"], content: string, meta?: Record<string, unknown>): ChatMessage {
  return { id: `${role}-${content.slice(0, 12)}`, role, content, timestamp: now, meta };
}

const richMarkdown = `## A settled batch of work

The header row in \`tool-card.tsx\` used a **fixed height** so badges never grow it — see [the docs](https://example.com) for details. Inline math like $E = mc^2$ renders through KaTeX.

- The first point, with \`inline code\` in it
- A second point that wraps onto another line when the column narrows enough to force it
- A third point

1. Ordered first
2. Ordered second

### Densities compared

| Surface | Row height | Padding |
| --- | --- | --- |
| Tool line | 24px | none |
| Card header | 28px | 10px |
| Table cell | auto | 6px |

> A blockquote reads as a quoted aside, indented behind a hairline rule — the same grammar the tool detail uses.

\`\`\`ts
export function fits(parent: Rect, child: Rect): boolean {
  return child.left >= parent.left && child.right <= parent.right;
}
\`\`\`

\`\`\`mermaid
flowchart LR
  prose[Markdown prose] --> line[Activity line]
  line -->|expand| rail[Detail rail]
  rail --> fields[Structured fields]
  line -.->|running| shimmer((Shimmer))
\`\`\`

A closing paragraph after the code block, to check the block gap on both sides.`;

const settledTools: ToolEvent[] = [
  {
    name: "read_file",
    toolCallId: "t1",
    status: "completed",
    arguments: { file_path: "web/src/components/tool-call.tsx", justification: "Reading the current `ToolCall` layout" },
    result: { content: "export function ToolCall() { /* … */ }" },
  },
  {
    name: "search_content",
    toolCallId: "t2",
    status: "completed",
    arguments: { pattern: "ToolCardHeader", path: "web/src" },
    result: { matches: ["web/src/components/tool-card.tsx:17"] },
  },
  {
    name: "edit_file",
    toolCallId: "t3",
    status: "completed",
    arguments: {
      file_path: "web/src/components/tool-card.tsx",
      old_string: "h={8}\npx={2.5}",
      new_string: "h={7}\npx={2.5}\ngap={1.5}",
      justification: "Tightening the card header",
      read_only: false,
    },
    result: { code: "ok" },
  },
  {
    name: "bash",
    toolCallId: "t4",
    status: "completed",
    arguments: { command: "bun run lint", read_only: false, risk: "medium" },
    result: { code: "bash_completed", output: "✓ no problems", pid: 4132 },
  },
];

const runningTools: ToolEvent[] = [
  {
    name: "web_search",
    toolCallId: "r1",
    status: "completed",
    arguments: { query: "chakra ui semantic tokens" },
    result: { results: [] },
  },
  {
    name: "bash",
    toolCallId: "r2",
    status: "running",
    arguments: { command: "bun run build", justification: "Building to verify the change", read_only: false },
  },
  {
    name: "spawn_agent",
    toolCallId: "r3",
    status: "running",
    arguments: { agent: "researcher" },
    result: { code: "background_task_scheduled" },
  },
];

const failedTool: ToolEvent = {
  name: "search_content",
  toolCallId: "f1",
  status: "failed",
  arguments: { pattern: "([unbalanced", path: "web/src" },
  result: { code: "tool_error", message: "regex parse error" },
};

const remoteTool: ToolEvent = {
  name: "bash",
  toolCallId: "s1",
  status: "completed",
  arguments: { command: "make deploy", location: "ssh://build-box/srv/app", read_only: false, risk: "high" },
  result: { output: "deployed" },
};

const unknownTool: ToolEvent = {
  name: "mcp_fetch_dashboard",
  toolCallId: "u1",
  status: "completed",
  arguments: { board: "main" },
  result: { code: "ok" },
};

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Box data-audit-section={title}>
      <Text textStyle="sectionLabel" mb={2} textTransform="uppercase" letterSpacing="0.05em">
        {title}
      </Text>
      {children}
    </Box>
  );
}

export default function GalleryPage() {
  const { colorMode, setColorMode } = useColorMode();
  // The running group is stateful so a driven browser can append calls and
  // measure how the heading (label crossfade, trailing tally) settles — the
  // exact motion a live turn produces, reproduced on demand.
  const [liveTools, setLiveTools] = useState<ToolEvent[]>(runningTools);
  const appendLiveTool = () =>
    setLiveTools((current) => [
      ...current,
      {
        name: "read_file",
        toolCallId: `live-${current.length}`,
        status: "running",
        arguments: { file_path: `web/src/components/pass-${current.length}.tsx`, justification: `Reading pass ${current.length} of the layout` },
      },
    ]);
  return (
    <Box minH="100dvh" bg="bg" px={4} py={6}>
      <Flex align="center" justify="space-between" maxW="760px" mx="auto" mb={6}>
        <Text textStyle="panelTitle">Design gallery</Text>
        <Text
          as="button"
          textStyle="fieldLabel"
          color="fg.muted"
          cursor="pointer"
          data-audit="color-mode-toggle"
          onClick={() => setColorMode(colorMode === "dark" ? "light" : "dark")}
        >
          {colorMode === "dark" ? "Switch to light" : "Switch to dark"}
        </Text>
      </Flex>
      {/* The transcript column mirrors ChatPanel's timeline: same VStack gap, so
          what is measured here is what the chat renders. */}
      <VStack gap={2.5} align="stretch" maxW="760px" mx="auto" data-audit="timeline">
        <Box data-audit="user-message" display="flex" flexDirection="column">
          <ChatMessageItem message={message("user", "Could you tighten the tool cards so they blend with the prose?")} />
        </Box>
        <Box data-audit="group-settled" display="flex" flexDirection="column">
          <ToolGroup tools={settledTools} />
        </Box>
        <Box data-audit="assistant-markdown" display="flex" flexDirection="column">
          <ChatMessageItem message={message("assistant", richMarkdown)} />
        </Box>
        <Box data-audit="group-running" display="flex" flexDirection="column">
          <ToolGroup tools={liveTools} keepOpen />
        </Box>
        <Text
          as="button"
          textStyle="fieldLabel"
          color="fg.subtle"
          cursor="pointer"
          alignSelf="flex-start"
          data-audit="append-live-tool"
          onClick={appendLiveTool}
        >
          + simulate a streamed call
        </Text>
        {/* keepOpen marks the live streaming tail — the only state in which a
            reasoning phase renders at all. */}
        <Box data-audit="group-thinking" display="flex" flexDirection="column">
          <ToolGroup tools={[]} keepOpen />
        </Box>
        <Box data-audit="call-failed" display="flex" flexDirection="column">
          <ToolCall {...failedTool} />
        </Box>
        <Box data-audit="call-remote" display="flex" flexDirection="column">
          <ToolCall {...remoteTool} />
        </Box>
        <Box data-audit="call-unknown" display="flex" flexDirection="column">
          <ToolCall {...unknownTool} />
        </Box>
        <Box data-audit="error-message" display="flex" flexDirection="column">
          <ChatMessageItem
            message={message("error", "", {
              error: { code: "rate_limited", title: "Rate limited", message: "The provider rejected the request. Wait a moment and try again." },
            })}
          />
        </Box>
        <Box data-audit="warning-message" display="flex" flexDirection="column">
          <ChatMessageItem
            message={message("warning", "", {
              warning: { code: "context_low", title: "Context is filling up", message: "Older turns will be summarized soon." },
            })}
          />
        </Box>
        <Box data-audit="compaction-row" display="flex" flexDirection="column">
          <ChatMessageItem message={message("compaction", "", { status: "done", messagesBefore: 42, messagesAfter: 9 })} />
        </Box>
      </VStack>

      <VStack gap={6} align="stretch" maxW="760px" mx="auto" mt={10}>
        <Section title="Agent step body (activity lines nested in a card)">
          <Box data-audit="agent-step">
            <ToolCard>
              <ToolCardHeader
                icon={<Box color="purple.fg"><LuSparkles size={13} /></Box>}
                title="researcher — Survey the design system"
                collapsible
                open
                onToggle={() => {}}
              />
              <ToolCardBody>
                <VStack gap={1} align="stretch" pt={0.5}>
                  <MarkdownContent content={"Scanning the theme first, then the call sites."} />
                  <ToolGroup tools={settledTools.slice(0, 3)} />
                </VStack>
              </ToolCardBody>
            </ToolCard>
          </Box>
        </Section>
        <Section title="Panel card shell (agents panel, skills)">
          <Box data-audit="panel-card">
            <ToolCard>
              <ToolCardHeader
                icon={<Box color="pink.fg"><LuSparkles size={13} /></Box>}
                title="A skill card title"
                collapsible
                open
                onToggle={() => {}}
              />
              <ToolCardBody>
                <ToolMetaRow label="Source">daisy/skills/example</ToolMetaRow>
                <ToolMetaRow label="Tools" mt={1}>bash, read_file</ToolMetaRow>
              </ToolCardBody>
            </ToolCard>
          </Box>
        </Section>
        <Section title="Compact markdown (tool detail scale)">
          <Box data-audit="markdown-xs" maxW="560px">
            <MarkdownContent content={"A `justification` rendered at the compact field scale, with **bold** and a [link](https://example.com)."} fontSize="xs" />
          </Box>
        </Section>
      </VStack>
    </Box>
  );
}
