"use client";

import { Box, Flex, IconButton, Link, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { LuAppWindow, LuRotateCw } from "react-icons/lu";
import ReactDiffViewer, { DiffMethod } from "react-diff-viewer-continued";
import { useColorMode } from "../ui/color-mode";
import { type A2ATask, taskArtifactText } from "@/lib/use-chat";
import { filePreviewUrl, proxyPreviewUrl } from "@/lib/api";
import { useWidgetEvent } from "../widget-bridge";
import { MarkdownContent } from "../markdown-content";
import {
  asArray,
  asRecord,
  asString,
  Card,
  EmptyHint,
  Field,
  FieldList,
  InlineField,
  MonoBlock,
  Pill,
} from "./primitives";

function stripCdPrefix(command: string): string {
  const match = command.match(/^cd\s+'[^']*'\s+&&\s+(.*)/s);
  return match ? match[1] : command;
}

function tryParse(content: string): unknown {
  try {
    return JSON.parse(content);
  } catch {
    return null;
  }
}

// Tool call (input) views

// Display label for each bash risk level. Falls back to the raw value when
// unmapped so an unexpected level still renders something readable.
const RISK_LABELS: Record<string, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
};

function BashCallView({ args }: { args: Record<string, unknown> }) {
  const command = stripCdPrefix(asString(args.command));
  const readOnly = args.read_only !== false;
  const risk = asString(args.risk) || "low";
  const riskText = RISK_LABELS[risk] ?? risk;
  return (
    <FieldList>
      <Field label="Command">
        <MonoBlock>{command}</MonoBlock>
      </Field>
      <InlineField label="Read only">{readOnly ? "Yes" : "No"}</InlineField>
      <InlineField label="Risk">{riskText}</InlineField>
    </FieldList>
  );
}

function WebSearchCallView({ args }: { args: Record<string, unknown> }) {
  return (
    <FieldList>
      <Field label="Query">
        <Text fontSize="xs">{asString(args.query)}</Text>
      </Field>
      {args.result_count != null && (
        <InlineField label="Results">{asString(args.result_count)}</InlineField>
      )}
    </FieldList>
  );
}

function agentLabelFor(agentName: string, agents: { id: string; name: string; title?: string }[]): string {
  const agent = agents.find((candidate) => candidate.id === agentName);
  return agent?.title || agent?.name || agentName || "Agent";
}

function SpawnAgentCallView({ args, agents }: { args: Record<string, unknown>; agents: { id: string; name: string; title?: string }[] }) {
  const agentName = asString(args.agent) || "assistant";
  return (
    <FieldList>
      <InlineField label="Agent">
        <Pill colorPalette="purple">{agentLabelFor(agentName, agents)}</Pill>
      </InlineField>
      <Field label="Prompt">
        <MarkdownContent content={asString(args.prompt)} fontSize="xs" />
      </Field>
    </FieldList>
  );
}


// A task's lifecycle status → a capitalized label and a colour, so it reads as a
// proper badge instead of the raw lowercase value the model emits.
function taskStatusAppearance(status: string): { label: string; palette: string } | null {
  switch (status.toLowerCase().replace(/[\s-]+/g, "_")) {
    case "completed": return null;
    case "in_progress": return { label: "In progress", palette: "blue" };
    case "blocked": return { label: "Blocked", palette: "yellow" };
    case "cancelled":
    case "canceled": return { label: "Cancelled", palette: "gray" };
    case "deleted": return { label: "Deleted", palette: "red" };
    case "pending": case "": return { label: "Pending", palette: "gray" };
    default: return { label: "Unknown", palette: "gray" };
  }
}

// "task-7" -> "#7" — the internal id is never shown to the user, only its number.
function taskHashLabel(id: string): string {
  const match = id.match(/(\d+)\s*$/);
  return match ? `#${match[1]}` : id;
}

// One row shared by task creation and task updates so both read identically: the
// task's #number on the left, a status badge on the right, the prose as markdown,
// and dependencies as #number chips (the dependency links carry the ordering).
function TaskRow({ label, status, body, dependencies = [] }: {
  label: string;
  status: string;
  body: string;
  dependencies?: string[];
}) {
  const appearance = taskStatusAppearance(status);
  return (
    <Card>
      <Flex align="center" gap={2} mb={body ? 1.5 : 0}>
        <Text fontSize="xs" color="fg.muted" fontWeight="semibold" flexShrink={0}>{label}</Text>
        <Box flex={1} />
        {appearance && <Pill colorPalette={appearance.palette}>{appearance.label}</Pill>}
      </Flex>
      {body && <MarkdownContent content={body} fontSize="xs" />}
      {dependencies.length > 0 && (
        <Flex align="center" gap={1} mt={1.5} flexWrap="wrap">
          <Text fontSize="2xs" color="fg.subtle">depends on</Text>
          {dependencies.map((dependency) => (
            <Pill key={dependency} colorPalette="purple">{taskHashLabel(dependency)}</Pill>
          ))}
        </Flex>
      )}
    </Card>
  );
}

function WriteTasksCallView({ args }: { args: Record<string, unknown> }) {
  const tasks = asArray(args.tasks).map(asRecord);
  return (
    <FieldList>
      {tasks.map((task, index) => (
        <TaskRow
          key={index}
          label={`#${index + 1}`}
          status="pending"
          body={asString(task.description)}
          dependencies={asArray(task.dependencies).map(asString)}
        />
      ))}
    </FieldList>
  );
}

function UpdateTasksCallView({ args }: { args: Record<string, unknown> }) {
  const updates = asArray(args.updates).map(asRecord);
  return (
    <FieldList>
      {updates.map((update, index) => (
        <TaskRow
          key={index}
          label={taskHashLabel(asString(update.task_id))}
          status={asString(update.status)}
          body={asString(update.result)}
        />
      ))}
    </FieldList>
  );
}

// The preview renders as the artifact (outside the card), so the call view just
// names what is being previewed (its URL/path) — never the page contents.
function WebPreviewCallView({ args }: { args: Record<string, unknown> }) {
  const url = asString(args.url);
  const title = asString(args.title);
  const summary = asString(args.summary);
  return (
    <FieldList>
      {title && <InlineField label="Title">{title}</InlineField>}
      {url && <InlineField label="Source">{url}</InlineField>}
      {summary && (
        <Field label="Summary">
          <MarkdownContent content={summary} fontSize="xs" />
        </Field>
      )}
    </FieldList>
  );
}

function ReadTaskCallView({ args }: { args: Record<string, unknown> }) {
  const taskId = asString(args.task_id);
  return (
    <FieldList>
      <InlineField label="Task ID">{taskId || "—"}</InlineField>
    </FieldList>
  );
}

// Fields whose values are human prose (not identifiers/data) — rendered with the
// markdown renderer in the normal font rather than monospace.
const PROSE_FIELD_KEYS = new Set([
  "justification",
  "description",
  "goal",
  "prompt",
  "reason",
  "summary",
  "message",
  "content",
  "instructions",
  "query",
]);

// Readable labels for raw argument/result keys. Falls back to the raw key if unmapped.
const FIELD_LABELS: Record<string, string> = {
  server: "Server",
  tool_name: "Tool name",
  arguments: "Arguments",
  read_only: "Read only",
  justification: "Justification",
  risk: "Risk",
  uri: "URI",
  query: "Query",
  result_count: "Results",
  agent: "Agent",
  prompt: "Prompt",
  task_id: "Task ID",
  task_identifier: "Task ID",
  code: "Status",
  // file / search tools (arguments)
  file_path: "File path",
  offset: "Offset",
  limit: "Limit",
  pattern: "Pattern",
  include: "Include",
  path: "Path",
  start_line: "Start line",
  end_line: "End line",
  new_lines: "New lines",
  content: "Content",
  url: "URL",
  format: "Format",
  timeout: "Timeout",
  name: "Name",
  questions: "Questions",
  options: "Options",
  header: "Header",
  multiple: "Multiple",
  custom: "Custom",
  // file / search tools (results)
  created: "Created",
  characters: "Characters",
  count: "Count",
  matches: "Matches",
  entries: "Entries",
  truncated: "Truncated",
  total_lines: "Total lines",
  sha256: "SHA-256",
  is_directory: "Directory",
  title: "Title",
  artifact: "Artifact",
  answers: "Answers",
};

// Monospace inline span for identifiers/paths/patterns/URLs — the scalar values
// that should read as code rather than prose.
function Mono({ children }: { children: ReactNode }) {
  return (
    <Text as="span" fontFamily="var(--app-font-mono)" fontSize="xs" wordBreak="break-all">
      {children}
    </Text>
  );
}

function ReadFileCallView({ args }: { args: Record<string, unknown> }) {
  return (
    <FieldList>
      <InlineField label="File path">
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      {args.offset != null && <InlineField label="Offset">{asString(args.offset)}</InlineField>}
      {args.limit != null && <InlineField label="Limit">{asString(args.limit)}</InlineField>}
    </FieldList>
  );
}

function ReplaceLinesCallView({ args }: { args: Record<string, unknown> }) {
  const newLines = Array.isArray(args.new_lines) ? args.new_lines.map(asString).join("\n") : asString(args.new_lines);

  return (
    <FieldList>
      <InlineField label="File path">
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      <InlineField label="Start line">{asString(args.start_line)}</InlineField>
      <InlineField label="End line">{asString(args.end_line)}</InlineField>
      <Field label="New lines">
        <MonoBlock>{newLines || " "}</MonoBlock>
      </Field>
    </FieldList>
  );
}

function WriteFileCallView({ args }: { args: Record<string, unknown> }) {
  return (
    <FieldList>
      <InlineField label="File path">
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      <Field label="Content">
        <MonoBlock>{asString(args.content)}</MonoBlock>
      </Field>
    </FieldList>
  );
}

function SearchContentCallView({ args }: { args: Record<string, unknown> }) {
  return (
    <FieldList>
      <InlineField label="Pattern">
        <Mono>{asString(args.pattern)}</Mono>
      </InlineField>
      {args.include ? (
        <InlineField label="Include">
          <Mono>{asString(args.include)}</Mono>
        </InlineField>
      ) : null}
      {args.path ? (
        <InlineField label="Path">
          <Mono>{asString(args.path)}</Mono>
        </InlineField>
      ) : null}
    </FieldList>
  );
}

function FindFilesCallView({ args }: { args: Record<string, unknown> }) {
  return (
    <FieldList>
      <InlineField label="Pattern">
        <Mono>{asString(args.pattern)}</Mono>
      </InlineField>
    </FieldList>
  );
}

function FetchUrlCallView({ args }: { args: Record<string, unknown> }) {
  return (
    <FieldList>
      <InlineField label="URL">
        <Mono>{asString(args.url)}</Mono>
      </InlineField>
      {args.format ? <InlineField label="Format">{asString(args.format)}</InlineField> : null}
      {args.timeout != null && <InlineField label="Timeout">{asString(args.timeout)}s</InlineField>}
    </FieldList>
  );
}

function LoadSkillCallView({ args }: { args: Record<string, unknown> }) {
  return (
    <FieldList>
      <InlineField label="Skill">
        <Mono>{asString(args.name)}</Mono>
      </InlineField>
    </FieldList>
  );
}

function AskUserCallView({ args }: { args: Record<string, unknown> }) {
  const questions = asArray(args.questions).map(asRecord);
  if (questions.length === 0) return null;
  return (
    <FieldList>
      {questions.map((item, index) => {
        const options = asArray(item.options).map(asRecord);
        const label = asString(item.header) || `Question ${index + 1}`;
        return (
          <Field key={index} label={label}>
            <Text fontSize="xs" mb={options.length ? 1.5 : 0}>
              {asString(item.question)}
            </Text>
            {options.length > 0 ? (
              <Flex wrap="wrap" gap={1}>
                {options.map((option, optionIndex) => (
                  <Pill key={optionIndex} colorPalette="blue">
                    {asString(option.label)}
                  </Pill>
                ))}
              </Flex>
            ) : null}
            {item.multiple === true ? (
              <Text fontSize="2xs" color="fg.subtle">
                multi-select
              </Text>
            ) : null}
          </Field>
        );
      })}
    </FieldList>
  );
}

function ReadFileResultView({ data }: { data: Record<string, unknown> }) {
  // The call already shows the file path, so the result only surfaces the line
  // range and the content (no duplicated Path field).
  if (data.is_directory === true) {
    const entries = asArray(data.entries).map(asString);
    return (
      <FieldList>
        <Field label="Entries">
          <MonoBlock>{entries.join("\n") || " "}</MonoBlock>
        </Field>
      </FieldList>
    );
  }
  const content = asString(data.content);
  const range = [asString(data.start_line), asString(data.end_line)].filter(Boolean).join("–");
  const total = asString(data.total_lines);
  return (
    <FieldList>
      {range && (
        <InlineField label="Lines">
          {range}
          {total ? ` of ${total}` : ""}
        </InlineField>
      )}
      {content && (
        <Field label="Content">
          <MonoBlock>{content}</MonoBlock>
        </Field>
      )}
    </FieldList>
  );
}

function MatchListResultView({ data }: { data: Record<string, unknown> }) {
  // Pattern is already shown on the call card — only surface the count + matches.
  const matches = asArray(data.matches).map(asString);
  const count = asString(data.count) || String(matches.length);
  return (
    <FieldList>
      <InlineField label="Count">{count}</InlineField>
      {matches.length > 0 && (
        <Field label="Matches">
          <MonoBlock>{matches.join("\n")}</MonoBlock>
        </Field>
      )}
    </FieldList>
  );
}

function FileEditResultView({ data }: { data: Record<string, unknown> }) {
  // Shared by replace_lines and write_file. Path is on the call card; the
  // internal `code` status is dropped.
  const characters = asString(data.characters);
  const before = asString(data.before);
  const after = asString(data.after);
  const { colorMode } = useColorMode();
  const hasDiff = before !== after && (before !== "" || after !== "");
  return (
    <FieldList>
      {data.created != null && (
        <InlineField label="Created">{data.created ? "Yes" : "No"}</InlineField>
      )}
      {characters && <InlineField label="Characters">{characters}</InlineField>}
      {hasDiff ? (
        <Box
          mt={1}
          maxH="520px"
          overflowY="auto"
          border="1px solid"
          borderColor="border"
          borderRadius="sm"
          className="diff-scroll"
        >
          <ReactDiffViewer
            oldValue={before}
            newValue={after}
            splitView={false}
            useDarkTheme={colorMode === "dark"}
            hideLineNumbers={false}
            showDiffOnly={false}
            compareMethod={DiffMethod.LINES}
            styles={{ contentText: { fontSize: "12px", fontFamily: "var(--app-font-mono)" } }}
          />
        </Box>
      ) : null}
    </FieldList>
  );
}

function FetchUrlResultView({ data }: { data: Record<string, unknown> }) {
  // URL + format are on the call card; only surface truncation + fetched content.
  const content = asString(data.content);
  return (
    <FieldList>
      {data.truncated === true && <InlineField label="Truncated">Yes</InlineField>}
      {content && (
        <Field label="Content">
          <MarkdownContent content={content} fontSize="xs" />
        </Field>
      )}
    </FieldList>
  );
}

function LoadSkillResultView({ data }: { data: Record<string, unknown> }) {
  // Skill name is on the call card; the internal resolved `path` is dropped.
  const title = asString(data.title);
  const content = asString(data.content);
  return (
    <FieldList>
      {title && <InlineField label="Title">{title}</InlineField>}
      {content && (
        <Field label="Content">
          <MarkdownContent content={content} fontSize="xs" />
        </Field>
      )}
    </FieldList>
  );
}

function AskUserResultView({ data }: { data: Record<string, unknown> }) {
  // Answers arrive as a per-question array of labels (string | string[]). Flatten
  // into readable pills instead of dumping the raw JSON array.
  const answers = asArray(data.answers);
  const labels: string[] = [];
  for (const answer of answers) {
    if (Array.isArray(answer)) labels.push(...answer.map(asString));
    else labels.push(asString(answer));
  }
  const shown = labels.filter(Boolean);
  if (shown.length === 0) return <EmptyHint>No answer</EmptyHint>;
  return (
    <FieldList>
      <Field label="Answers">
        <Flex wrap="wrap" gap={1}>
          {shown.map((label, index) => (
            <Pill key={index} colorPalette="green">
              {label}
            </Pill>
          ))}
        </Flex>
      </Field>
    </FieldList>
  );
}

function GenericView({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) return <EmptyHint>No data</EmptyHint>;
  return (
    <FieldList>
      {entries.map(([key, value]) => (
        <InlineField key={key} label={FIELD_LABELS[key] ?? key}>
          {value && typeof value === "object" ? (
            // Structured values (objects/arrays) are data — monospace JSON.
            <MonoBlock>{JSON.stringify(value, null, 2)}</MonoBlock>
          ) : PROSE_FIELD_KEYS.has(key) ? (
            // Prose values render as markdown, sized to match the compact field context.
            <MarkdownContent content={asString(value)} fontSize="xs" />
          ) : (
            // Scalar identifiers/data (names, ids, flags) render in monospace.
            <Text fontSize="xs" fontFamily="var(--app-font-mono)" whiteSpace="pre-wrap">{asString(value)}</Text>
          )}
        </InlineField>
      ))}
    </FieldList>
  );
}

export function ToolCallView({ name, args, agents = [] }: { name: string; args?: Record<string, unknown>; agents?: { id: string; name: string; title?: string }[] }) {
  if (!args) return null;
  switch (name) {
    case "bash":
      return <BashCallView args={args} />;
    case "web_search":
      return <WebSearchCallView args={args} />;
    case "spawn_agent":
      return <SpawnAgentCallView args={args} agents={agents} />;
    case "write_tasks":
      return <WriteTasksCallView args={args} />;
    case "update_tasks":
      return <UpdateTasksCallView args={args} />;
    case "read_task":
      return <ReadTaskCallView args={args} />;
    case "open_web_preview":
      return <WebPreviewCallView args={args} />;
    case "read_file":
      return <ReadFileCallView args={args} />;
    case "replace_lines":
      return <ReplaceLinesCallView args={args} />;
    case "write_file":
      return <WriteFileCallView args={args} />;
    case "search_content":
      return <SearchContentCallView args={args} />;
    case "find_files":
      return <FindFilesCallView args={args} />;
    case "fetch_url":
      return <FetchUrlCallView args={args} />;
    case "load_skill":
      return <LoadSkillCallView args={args} />;
    case "ask_user":
      return <AskUserCallView args={args} />;
    default:
      return <GenericView data={args} />;
  }
}

// Tool result (output) views

function BashResultView({ data }: { data: Record<string, unknown> }) {
  const output = asString(data.output);
  const outputFile = asString(data.output_file);
  const hasMeta = data.pid != null || data.size != null;
  if (!output && !outputFile && !hasMeta) return null;
  return (
    <FieldList>
      {data.pid != null && <InlineField label="PID">{asString(data.pid)}</InlineField>}
      {data.size != null && <InlineField label="Size">{asString(data.size)} bytes</InlineField>}
      {output ? (
        <Field label="Output">
          <MonoBlock>{output}</MonoBlock>
        </Field>
      ) : outputFile ? (
        <InlineField label="Output">written to {outputFile}</InlineField>
      ) : null}
    </FieldList>
  );
}

function WebResultCard({ result }: { result: Record<string, unknown> }) {
  const title = asString(result.title) || "Untitled";
  const url = asString(result.url);
  const summary = asString(result.summary);
  const date = asString(result.published_date);
  return (
    <Card>
      {url ? (
        <Link href={url} target="_blank" rel="noopener noreferrer" colorPalette="blue" fontSize="xs" fontWeight="medium">
          {title}
        </Link>
      ) : (
        <Text fontSize="xs" fontWeight="medium">{title}</Text>
      )}
      {url && (
        <Text fontSize="xs" color="fg.subtle" fontFamily="var(--app-font-mono)" truncate>{url}</Text>
      )}
      {date && (
        <Text fontSize="2xs" color="fg.subtle">{date}</Text>
      )}
      {summary && (
        <Box mt={1} color="fg.muted">
          <MarkdownContent content={summary} fontSize="xs" />
        </Box>
      )}
    </Card>
  );
}

// The query and requested count are already shown by the call view above, so the
// result view only renders the result cards — shown directly, not behind a
// nested collapsible (the tool-call card is the collapsible).
function WebSearchResultView({ data }: { data: Record<string, unknown> }) {
  const results = asArray(data.results).map(asRecord);
  if (results.length === 0) return <EmptyHint>No results</EmptyHint>;
  return (
    <Flex direction="column" gap={1.5}>
      {results.map((result, index) => <WebResultCard key={index} result={result} />)}
    </Flex>
  );
}

// A spawned sub-agent returns its A2A task as the tool result; show its
// deliverable (artifact).
function AgentTaskResultView({ data }: { data: Record<string, unknown> }) {
  const task = data as unknown as A2ATask;
  return (
    <FieldList>
      <Field label="Response">
        <MarkdownContent content={taskArtifactText(task)} />
      </Field>
    </FieldList>
  );
}

function ErrorView({ message }: { message: string }) {
  return (
    <Box bg="red.subtle" border="1px solid" borderColor="red.muted" borderRadius="sm" px={2} py={1.5}>
      <Text fontSize="xs" color="red.fg">{message}</Text>
    </Box>
  );
}

// read_task returns either an error code (no id match / unavailable) or the
// task object itself (kind === "task"). The id is already shown on the call
// card, so the result only surfaces the outcome — it never re-renders the id.
function ReadTaskResultView({ data }: { data: Record<string, unknown> }) {
  const code = asString(data.code);
  if (code === "task_not_found") return <ErrorView message="No task with that id." />;
  if (code === "read_task_unavailable") {
    return <EmptyHint>Reading tasks is not available in this context.</EmptyHint>;
  }
  return <AgentTaskResultView data={data} />;
}

const IFRAME_SANDBOX_TOKENS = new Set([
  "allow-downloads",
  "allow-forms",
  "allow-modals",
  "allow-popups",
  "allow-popups-to-escape-sandbox",
  "allow-presentation",
  "allow-same-origin",
  "allow-scripts",
]);

function safeWebUrl(value: string): string {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : "";
  } catch {
    return "";
  }
}

function safeImageSource(value: string): string {
  if (value.startsWith("data:image/")) return value;
  return safeWebUrl(value);
}

function artifactHeight(value: unknown): string {
  const numeric = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(numeric)) return "360px";
  return `${Math.max(160, Math.min(720, numeric))}px`;
}

function artifactSandbox(artifact: Record<string, unknown>, usesInlineHtml: boolean): string {
  const requestedTokens = asString(artifact.sandbox).split(/\s+/).filter(Boolean);
  const filteredTokens = requestedTokens.filter((token) => (
    IFRAME_SANDBOX_TOKENS.has(token) && !(usesInlineHtml && token === "allow-same-origin")
  ));
  if (filteredTokens.length > 0) return filteredTokens.join(" ");
  return usesInlineHtml ? "allow-scripts allow-popups" : "allow-scripts allow-same-origin allow-popups";
}

function normalizeArtifact(value: unknown): Record<string, unknown> | null {
  const artifact = asRecord(value);
  if (Object.keys(artifact).length === 0) return null;
  let type = asString(artifact.type).toLowerCase();
  if (!type) {
    if (asString(artifact.src) || asString(artifact.srcdoc) || asString(artifact.file)) type = "iframe";
    else if (asString(artifact.html)) type = "html";
    else if (asString(artifact.data) || asString(artifact.url)) type = "image";
  }
  if (!["iframe", "html", "image", "link"].includes(type)) return null;
  return { ...artifact, type };
}

function collectArtifacts(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.flatMap(collectArtifacts);
  const direct = normalizeArtifact(value);
  return direct ? [direct] : [];
}

function compactMcpContent(content: unknown): unknown {
  return asArray(content).map((entry) => {
    const record = asRecord(entry);
    if (record.type === "image") {
      return { type: "image", mime_type: record.mimeType || record.mime_type };
    }
    if (record.type === "resource") {
      const resource = asRecord(record.resource);
      return {
        type: "resource",
        uri: resource.uri,
        mime_type: resource.mimeType || resource.mime_type,
      };
    }
    if (record.uri && (record.mimeType || record.mime_type)) {
      const mimeType = asString(record.mimeType || record.mime_type);
      if (mimeType.startsWith("image/") || mimeType === "text/html" || mimeType === "application/xhtml+xml") {
        return {
          type: "resource",
          uri: record.uri,
          mime_type: mimeType,
        };
      }
    }
    return entry;
  });
}

// An artifact's title/label may contain markdown, so it is rendered through the
// markdown renderer above the artifact body.
function ArtifactFrame({ title, children }: { title: string; children: ReactNode }) {
  // Remounting the content (via a changing key) forces a fresh fetch of the
  // previewed page — the /preview route is served no-store, so the iframe reloads
  // the current file/URL rather than showing a stale render. Works for every
  // artifact kind (iframe, inline html, image) since each is a child subtree.
  const [reloadKey, setReloadKey] = useState(0);
  return (
    <Box>
      <Flex align="center" gap={1.5} mb={1.5}>
        <Text fontSize="sm" fontWeight="semibold" color="fg.muted" flex={1} minW={0} truncate>
          {title}
        </Text>
        <IconButton
          aria-label="Reload preview"
          title="Reload preview"
          size="xs"
          variant="ghost"
          borderRadius="sm"
          h="20px"
          minW="20px"
          px={1}
          flexShrink={0}
          onClick={() => setReloadKey((current) => current + 1)}
        >
          <LuRotateCw size={10} />
        </IconButton>
      </Flex>
      <Box key={reloadKey}>{children}</Box>
    </Box>
  );
}

// A generous floor: model-authored widgets frequently mis-measure their own
// height (absolute-positioned maps, late-loading content) and collapse to a few
// pixels. Never render one shorter than this, whether auto-sized or hand-resized.
const AUTO_MINIMUM_HEIGHT = 480;
const AUTO_MAXIMUM_HEIGHT = 960;

// A sandboxed iframe (in a bordered frame) that listens for back-channel messages
// the widget posts to its parent. Messages are accepted only from this frame's own
// contentWindow and only when tagged `source: "harness-widget"`. Two kinds:
//   - `__widget_resize` → sizes the frame to the content (when autoHeight), so the
//     model never has to guess a height. Never forwarded to the agent.
//   - anything else → a real interaction, forwarded (with the widget's id/title)
//     to the active WidgetEvent handler, becoming a follow-up turn for the agent.
function WidgetFrame({
  src,
  srcDoc,
  sandbox,
  title,
  artifactId,
  autoHeight,
  fixedHeight,
}: {
  src?: string;
  srcDoc?: string;
  sandbox: string;
  title: string;
  artifactId: string;
  autoHeight: boolean;
  fixedHeight: string;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const onWidgetEvent = useWidgetEvent();
  const [measuredHeight, setMeasuredHeight] = useState<number | null>(null);

  useEffect(() => {
    function handleMessage(messageEvent: MessageEvent) {
      const frame = frameRef.current;
      if (!frame || messageEvent.source !== frame.contentWindow) return;
      const payload = messageEvent.data;
      if (!payload || typeof payload !== "object") return;
      const record = payload as Record<string, unknown>;
      if (record.source !== "harness-widget") return;
      if (record.event === "__widget_resize") {
        if (autoHeight) {
          const reported = Number((record.data as Record<string, unknown> | undefined)?.height);
          if (Number.isFinite(reported)) {
            setMeasuredHeight(Math.max(AUTO_MINIMUM_HEIGHT, Math.min(AUTO_MAXIMUM_HEIGHT, Math.ceil(reported))));
          }
        }
        return; // internal sizing signal — never an agent-facing event
      }
      if (!onWidgetEvent) return;
      const eventName = typeof record.event === "string" ? record.event.trim() : "";
      if (!eventName) return;
      onWidgetEvent({ artifactId, title, event: eventName, data: record.data });
    }
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [onWidgetEvent, artifactId, title, autoHeight]);

  // A manual drag overrides auto-sizing entirely — the model's own resize logic
  // often collapses or mis-sizes a widget, so the user can always take over.
  const [userHeight, setUserHeight] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const baseHeight = Math.max(
    AUTO_MINIMUM_HEIGHT,
    autoHeight ? (measuredHeight ?? AUTO_MINIMUM_HEIGHT) : (parseInt(fixedHeight, 10) || 480),
  );
  const effectiveHeight = userHeight ?? baseHeight;

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = effectiveHeight;
    setDragging(true);
    function onMove(moveEvent: PointerEvent) {
      setUserHeight(Math.max(AUTO_MINIMUM_HEIGHT, Math.round(startHeight + (moveEvent.clientY - startY))));
    }
    function onUp() {
      setDragging(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  return (
    <Box position="relative" w="100%">
      <Box
        w="100%"
        h={`${effectiveHeight}px`}
        border="1px solid"
        borderColor="border"
        borderRadius="sm"
        bg="bg.muted"
        overflow="hidden"
        transition={dragging ? undefined : "height 120ms ease"}
      >
        <iframe
          ref={frameRef}
          src={src || undefined}
          srcDoc={srcDoc || undefined}
          sandbox={sandbox}
          referrerPolicy="no-referrer"
          loading="lazy"
          title={title}
          // Ignore pointer events on the iframe mid-drag so it doesn't swallow them.
          style={{ width: "100%", height: "100%", border: 0, display: "block", pointerEvents: dragging ? "none" : "auto" }}
        />
      </Box>
      {/* Drag the grip to set a fixed height (overriding auto-resize). */}
      <Box
        onPointerDown={startResize}
        position="absolute"
        left={0}
        right={0}
        bottom="-4px"
        h="10px"
        cursor="ns-resize"
        display="flex"
        alignItems="center"
        justifyContent="center"
        role="separator"
        aria-label="Resize widget height"
        _hover={{ "& > div": { bg: "fg.muted" } }}
      />
    </Box>
  );
}

function RenderArtifact({ artifact }: { artifact: Record<string, unknown> }) {
  const type = asString(artifact.type);
  const title = asString(artifact.title) || "MCP artifact";
  const artifactId = asString(artifact.artifact_id) || asString(artifact.artifactId) || asString(artifact.id);
  const isAutoHeight = artifact.height === "auto" || artifact.height == null || artifact.height === "";
  if (type === "iframe") {
    // A `file` reference (open_web_preview of a local file) is served by the backend
    // /preview route, which injects the runtime so the page can self-size — so honor
    // auto-height for it, the same as inline html. An external `src` is routed through
    // the /preview-proxy route so sites that refuse direct framing still render.
    const file = asString(artifact.file);
    const externalSrc = safeWebUrl(asString(artifact.src));
    const baseSrc = file ? filePreviewUrl(file) : externalSrc ? proxyPreviewUrl(externalSrc) : "";
    // An in-place refresh (replace) keeps the same artifact_id (and the same file
    // path/URL), so the iframe src would be byte-identical and the frame would never
    // reload — showing the previous render until a full page reload. The backend
    // stamps a fresh `version` on every preview call; fold it into the src as a
    // cache-buster so a refresh actually reloads the (rewritten) page.
    const version = asString(artifact.version);
    const src = baseSrc && version
      ? `${baseSrc}${baseSrc.includes("?") ? "&" : "?"}v=${encodeURIComponent(version)}`
      : baseSrc;
    const srcDoc = asString(artifact.srcdoc);
    if (!src && !srcDoc) return <ErrorView message="Iframe artifact did not include a safe source." />;
    return (
      <ArtifactFrame title={title}>
        <WidgetFrame
          src={src || undefined}
          srcDoc={srcDoc || undefined}
          sandbox={artifactSandbox(artifact, Boolean(srcDoc))}
          title={title}
          artifactId={artifactId}
          autoHeight={Boolean(file) && isAutoHeight}
          // An external page can't report its height, so give it a generous default
          // frame (the user can still drag to resize) instead of the short fallback.
          fixedHeight={!file && isAutoHeight ? "640px" : artifactHeight(artifact.height)}
        />
      </ArtifactFrame>
    );
  }
  if (type === "html") {
    const html = asString(artifact.html) || asString(artifact.srcdoc);
    if (!html) return <ErrorView message="HTML artifact did not include content." />;
    return (
      <ArtifactFrame title={title}>
        <WidgetFrame
          srcDoc={html}
          sandbox={artifactSandbox(artifact, true)}
          title={title}
          artifactId={artifactId}
          autoHeight={isAutoHeight}
          fixedHeight={artifactHeight(artifact.height)}
        />
      </ArtifactFrame>
    );
  }
  if (type === "image") {
    const source = safeImageSource(asString(artifact.data) || asString(artifact.src) || asString(artifact.url));
    if (!source) return <ErrorView message="Image artifact did not include a safe source." />;
    return (
      <ArtifactFrame title={title}>
        <Box
          maxW="100%"
          maxH={artifactHeight(artifact.height)}
          border="1px solid"
          borderColor="border"
          borderRadius="sm"
          bg="bg.muted"
          overflow="hidden"
        >
          {/* eslint-disable-next-line @next/next/no-img-element -- MCP image artifacts can be data URLs or arbitrary remote sources. */}
          <img
            src={source}
            alt={title}
            style={{ maxWidth: "100%", maxHeight: artifactHeight(artifact.height), display: "block" }}
          />
        </Box>
      </ArtifactFrame>
    );
  }
  const href = safeWebUrl(asString(artifact.href) || asString(artifact.url) || asString(artifact.src));
  if (!href) return <ErrorView message="Link artifact did not include a safe URL." />;
  return (
    <ArtifactFrame title={title}>
      <Link href={href} target="_blank" rel="noopener noreferrer" colorPalette="blue">
        {href}
      </Link>
    </ArtifactFrame>
  );
}

// Renderable artifacts (maps, images, HTML) returned by an MCP tool/resource.
// These are rendered OUTSIDE the tool-call card so they stay visible, rather than
// being tucked inside its collapsible body.
export function extractToolArtifacts(name: string, content: string): Record<string, unknown>[] {
  // `render_widget` stays recognized so older persisted sessions still replay their
  // inline-html artifacts; new sessions use `open_web_preview`.
  if (name !== "call_mcp_tool" && name !== "read_mcp_resource" && name !== "render_widget" && name !== "open_web_preview") return [];
  const parsed = tryParse(content);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  return collectArtifacts((parsed as Record<string, unknown>).artifacts);
}

// Only iframe/html artifacts mount a live frame (scripts, network, layout). Images
// and links are cheap, so the "one live preview at a time" rule only applies here.
export function isLivePreviewArtifact(artifact: Record<string, unknown>): boolean {
  const normalized = normalizeArtifact(artifact);
  return normalized?.type === "iframe" || normalized?.type === "html";
}

// A collapsed, click-to-open stand-in for a live preview that is not currently
// active. Unmounting the iframe stops its scripts and network activity, which is
// what keeps a long transcript (many previews) from dragging the page down.
function CollapsedPreview({ title, onOpen }: { title: string; onOpen: () => void }) {
  return (
    <Flex
      as="button"
      align="center"
      gap={1.5}
      px={2}
      py={1.5}
      borderRadius="sm"
      border="1px solid"
      borderColor="border"
      bg="bg.subtle"
      cursor="pointer"
      textAlign="left"
      w="100%"
      onClick={onOpen}
      _hover={{ bg: "bg.muted", borderColor: "border.emphasized" }}
    >
      <Box color="fg.muted" flexShrink={0}>
        <LuAppWindow size={13} />
      </Box>
      <Text fontSize="xs" fontWeight="medium" flex={1} minW={0} truncate>{title}</Text>
      <Text fontSize="2xs" color="fg.subtle" flexShrink={0}>click to open</Text>
    </Flex>
  );
}

export function ToolArtifacts({
  artifacts,
  activePreviewId,
  onActivatePreview,
  toolCallId,
}: {
  artifacts: Record<string, unknown>[];
  // The toolCallId of the single live preview call (owned by ChatPanel, which
  // auto-activates the newest preview and lets the user click to reopen an older
  // one). null/undefined while none has been claimed yet — a transient that
  // resolves on the same render pass, so users never see two live previews at once.
  activePreviewId?: string | null;
  onActivatePreview?: (toolCallId: string) => void;
  toolCallId?: string;
}) {
  if (artifacts.length === 0) return null;
  // Identity here is just the owning tool call: one open_web_preview is one tool
  // call, so "this call is the active preview" is enough to decide mount-vs-collapse.
  const callActive = activePreviewId == null || activePreviewId === toolCallId;
  return (
    <Flex direction="column" gap={1.5}>
      {artifacts.map((artifact, index) => {
        const key = asString(artifact.artifact_id) || asString(artifact.artifactId) || asString(artifact.id) || String(index);
        if (isLivePreviewArtifact(artifact) && !callActive) {
          const title = asString(artifact.title) || "Preview";
          return <CollapsedPreview key={key} title={title} onOpen={() => onActivatePreview?.(toolCallId ?? "")} />;
        }
        return <RenderArtifact key={key} artifact={artifact} />;
      })}
    </Flex>
  );
}

// The MCP result shown inside the tool-call card. Artifacts are rendered
// separately (outside the card), so this only surfaces the tool's textual output.
function McpResultView({ data }: { data: Record<string, unknown> }) {
  if (data.is_error === true) {
    return <ErrorView message="The MCP tool returned an error." />;
  }
  const structuredContent = data.structured_content;
  const output = structuredContent != null ? structuredContent : compactMcpContent(data.content ?? data.contents);
  if (output == null || (Array.isArray(output) && output.length === 0)) {
    return null;
  }
  return (
    <FieldList>
      <MonoBlock>{JSON.stringify(output, null, 2)}</MonoBlock>
    </FieldList>
  );
}

export function ToolResultView({ name, content }: { name: string; content: string }) {
  const parsed = tryParse(content);

  // MCP discovery results (the full server/tool catalog with JSON schemas) are
  // internal noise — the call card already conveys that discovery happened.
  if (name === "list_mcp_tools" || name === "list_mcp_resources") return null;

  // The task tools' result is a bare confirmation string that names raw "task-N"
  // ids (for the model); the call card already shows the tasks as #N, so don't
  // re-render the confirmation and leak the internal ids.
  if (name === "write_tasks" || name === "update_tasks") return null;

  // A preview/widget's result is just compact model_context metadata — the rendered
  // artifact is the deliverable (it shows outside the card). `render_widget` is kept
  // for replay of older sessions.
  if (name === "render_widget" || name === "open_web_preview") return null;

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const data = parsed as Record<string, unknown>;
    const code = asString(data.code);
    // "scheduled" notices (web_search_started, bash_started, background_task_scheduled)
    // are transient implementation details — the matching *_completed result arrives
    // shortly and renders instead. Don't render the raw scheduling payload.
    if (code.endsWith("_started") || code === "background_task_scheduled") return null;
    if (code === "tool_error") return <ErrorView message={asString(data.message) || "Tool failed"} />;
    if (code === "web_search_completed") return <WebSearchResultView data={data} />;
    if (code === "web_search_error") return <ErrorView message={asString(data.message) || "Search failed"} />;
    if (code.startsWith("bash")) return <BashResultView data={data} />;
    if (name === "call_mcp_tool" || name === "read_mcp_resource") return <McpResultView data={data} />;
    if (asString(data.kind) === "task") return <AgentTaskResultView data={data} />;
    if (code === "empty_response") {
      const message = asString(data.message);
      return message ? <EmptyHint>{message}</EmptyHint> : null;
    }
    if (name === "read_task") return <ReadTaskResultView data={data} />;
    if (name === "read_file") return <ReadFileResultView data={data} />;
    if (name === "find_files" || name === "search_content") return <MatchListResultView data={data} />;
    if (name === "replace_lines" || name === "write_file") return <FileEditResultView data={data} />;
    if (name === "fetch_url") return <FetchUrlResultView data={data} />;
    if (name === "load_skill") return <LoadSkillResultView data={data} />;
    if (name === "ask_user") return <AskUserResultView data={data} />;
    return <GenericView data={data} />;
  }

  // Non-JSON results (e.g. a spawned agent's final text) render as prose.
  return <MarkdownContent content={content} />;
}
