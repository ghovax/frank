"use client";

import { Box, Flex, Link, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { type A2ATask, taskArtifactText } from "@/lib/use-chat";
import { MarkdownContent } from "../markdown-content";
import {
  asArray,
  asRecord,
  asString,
  Card,
  Collapsible,
  EmptyHint,
  Field,
  FieldList,
  InlineField,
  MonoBlock,
  Pill,
} from "./primitives";

function riskPalette(risk: string): string {
  if (risk === "high") return "red";
  if (risk === "medium") return "yellow";
  return "green";
}

function riskLabel(risk: string): string {
  if (risk === "high") return "High risk";
  if (risk === "medium") return "Medium risk";
  return "Low risk";
}

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

function BashCallView({ args }: { args: Record<string, unknown> }) {
  const command = stripCdPrefix(asString(args.command));
  const risk = asString(args.risk) || "low";
  const readOnly = args.read_only !== false;
  return (
    <FieldList>
      <Field label="Command">
        <MonoBlock>{command}</MonoBlock>
      </Field>
      <Flex gap={2}>
        <Pill colorPalette={readOnly ? "gray" : "orange"}>
          {readOnly ? "Read-only command" : "Can modify files"}
        </Pill>
        <Pill colorPalette={riskPalette(risk)}>{riskLabel(risk)}</Pill>
      </Flex>
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
        <MarkdownContent content={asString(args.prompt)} />
      </Field>
    </FieldList>
  );
}


function WriteTasksCallView({ args }: { args: Record<string, unknown> }) {
  const tasks = asArray(args.tasks).map(asRecord);
  return (
    <FieldList>
      {tasks.map((task, index) => {
        const dependencies = asArray(task.dependencies).map(asString);
        return (
          <Card key={index}>
            <Text fontSize="xs">{asString(task.description)}</Text>
            {dependencies.length > 0 && (
              <Text fontSize="2xs" color="fg.subtle" mt={1}>depends on {dependencies.join(", ")}</Text>
            )}
          </Card>
        );
      })}
    </FieldList>
  );
}

function UpdateTasksCallView({ args }: { args: Record<string, unknown> }) {
  const updates = asArray(args.updates).map(asRecord);
  return (
    <FieldList>
      {updates.map((update, index) => (
        <Card key={index}>
          <Flex align="center" gap={2}>
            <Text fontSize="xs" color="fg.muted">{asString(update.task_id)}</Text>
            <Pill colorPalette="blue">{asString(update.status)}</Pill>
          </Flex>
          {asString(update.result) && (
            <Text fontSize="xs" color="fg.muted" mt={1}>{asString(update.result)}</Text>
          )}
        </Card>
      ))}
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

// Readable labels for raw argument keys. Falls back to the raw key if unmapped.
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
};

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

export function ToolCallView({ name, args, agents = [] }: { name: string; args?: Record<string, unknown>; agents?: { id: string; name: string }[] }) {
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
    default:
      return <GenericView data={args} />;
  }
}

// Tool result (output) views

function BashResultView({ data }: { data: Record<string, unknown> }) {
  const output = asString(data.output);
  const outputFile = asString(data.output_file);
  const hasPills = data.pid != null || data.size != null;
  if (!output && !outputFile && !hasPills) return null;
  return (
    <FieldList>
      {hasPills && (
        <Flex gap={2}>
          {data.pid != null && <Pill>pid {asString(data.pid)}</Pill>}
          {data.size != null && <Pill>{asString(data.size)} bytes</Pill>}
        </Flex>
      )}
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

// A spawned sub-agent returns its A2A task as the tool result; show its terminal
// state and its deliverable (artifact).
function AgentTaskResultView({ data }: { data: Record<string, unknown> }) {
  const task = data as unknown as A2ATask;
  const state = task.status?.state;
  return (
    <FieldList>
      <Flex align="center" gap={2}>
        {state && <Pill colorPalette={state === "completed" ? "green" : state === "failed" ? "red" : "gray"}>{state}</Pill>}
      </Flex>
      <MarkdownContent content={taskArtifactText(task)} />
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
    if (asString(artifact.src) || asString(artifact.srcdoc)) type = "iframe";
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
  return (
    <Box>
      <Box mb={1} color="fg.subtle">
        <MarkdownContent content={title} fontSize="sm" />
      </Box>
      {children}
    </Box>
  );
}

function RenderArtifact({ artifact }: { artifact: Record<string, unknown> }) {
  const type = asString(artifact.type);
  const title = asString(artifact.title) || "MCP artifact";
  if (type === "iframe") {
    const src = safeWebUrl(asString(artifact.src));
    const srcDoc = asString(artifact.srcdoc);
    if (!src && !srcDoc) return <ErrorView message="Iframe artifact did not include a safe source." />;
    return (
      <ArtifactFrame title={title}>
        <Box
          w="100%"
          h={artifactHeight(artifact.height)}
          border="1px solid"
          borderColor="border"
          borderRadius="sm"
          bg="bg.muted"
          overflow="hidden"
        >
          <iframe
            src={src || undefined}
            srcDoc={srcDoc || undefined}
            sandbox={artifactSandbox(artifact, Boolean(srcDoc))}
            referrerPolicy="no-referrer"
            loading="lazy"
            title={title}
            style={{ width: "100%", height: "100%", border: 0, display: "block" }}
          />
        </Box>
      </ArtifactFrame>
    );
  }
  if (type === "html") {
    const html = asString(artifact.html) || asString(artifact.srcdoc);
    if (!html) return <ErrorView message="HTML artifact did not include content." />;
    return (
      <ArtifactFrame title={title}>
        <Box
          w="100%"
          h={artifactHeight(artifact.height)}
          border="1px solid"
          borderColor="border"
          borderRadius="sm"
          bg="bg.muted"
          overflow="hidden"
        >
          <iframe
            srcDoc={html}
            sandbox={artifactSandbox(artifact, true)}
            referrerPolicy="no-referrer"
            title={title}
            style={{ width: "100%", height: "100%", border: 0, display: "block" }}
          />
        </Box>
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
  if (name !== "call_mcp_tool" && name !== "read_mcp_resource") return [];
  const parsed = tryParse(content);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  return collectArtifacts((parsed as Record<string, unknown>).artifacts);
}

export function ToolArtifacts({ artifacts }: { artifacts: Record<string, unknown>[] }) {
  if (artifacts.length === 0) return null;
  return (
    <Flex direction="column" gap={1.5}>
      {artifacts.map((artifact, index) => {
        const key = asString(artifact.artifact_id) || asString(artifact.artifactId) || asString(artifact.id) || String(index);
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

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const data = parsed as Record<string, unknown>;
    const code = asString(data.code);
    // "scheduled" notices (web_search_started, bash_started, background_task_scheduled)
    // are transient implementation details — the matching *_completed result arrives
    // shortly and renders instead. Don't render the raw scheduling payload.
    if (code.endsWith("_started") || code === "background_task_scheduled") return null;
    if (code === "web_search_completed") return <WebSearchResultView data={data} />;
    if (code === "web_search_error") return <ErrorView message={asString(data.message) || "Search failed"} />;
    if (code.startsWith("bash")) return <BashResultView data={data} />;
    if (name === "call_mcp_tool" || name === "read_mcp_resource") return <McpResultView data={data} />;
    if (asString(data.kind) === "task") return <AgentTaskResultView data={data} />;
    if (code === "empty_response") {
      const message = asString(data.message);
      return message ? <EmptyHint>{message}</EmptyHint> : null;
    }
    return <GenericView data={data} />;
  }

  // Non-JSON results (e.g. a spawned agent's final text) render as prose.
  return <MarkdownContent content={content} />;
}
