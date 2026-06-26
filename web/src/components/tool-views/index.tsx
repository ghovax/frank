"use client";

import { Box, Flex, Link, Text } from "@chakra-ui/react";
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

function agentLabelFor(agentName: string, agents: { name: string; label: string }[]): string {
  return agents.find((agent) => agent.name === agentName)?.label || agentName || "Agent";
}

function SpawnAgentCallView({ args, agents }: { args: Record<string, unknown>; agents: { name: string; label: string }[] }) {
  const agentName = asString(args.agent) || "researcher";
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

function GenericView({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) return <EmptyHint>No data</EmptyHint>;
  return (
    <FieldList>
      {entries.map(([key, value]) => (
        <InlineField key={key} label={key}>
          {value && typeof value === "object" ? (
            <MonoBlock>{JSON.stringify(value, null, 2)}</MonoBlock>
          ) : (
            <Text fontSize="xs" whiteSpace="pre-wrap">{asString(value)}</Text>
          )}
        </InlineField>
      ))}
    </FieldList>
  );
}

export function ToolCallView({ name, args, agents = [] }: { name: string; args?: Record<string, unknown>; agents?: { name: string; label: string }[] }) {
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
  return (
    <FieldList>
      <Flex gap={2}>
        {data.pid != null && <Pill>pid {asString(data.pid)}</Pill>}
        {data.size != null && <Pill>{asString(data.size)} bytes</Pill>}
      </Flex>
      {output ? (
        <Field label="Output">
          <MonoBlock>{output}</MonoBlock>
        </Field>
      ) : outputFile ? (
        <InlineField label="Output">written to {outputFile}</InlineField>
      ) : (
        <EmptyHint>No output</EmptyHint>
      )}
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
        <Text fontSize="2xs" color="fg.subtle" truncate>{url}</Text>
      )}
      {date && (
        <Text fontSize="2xs" color="fg.subtle">{date}</Text>
      )}
      {summary && (
        <Box mt={1}>
          <Collapsible title="Summary">
            <Text fontSize="xs" color="fg.muted" whiteSpace="pre-wrap">{summary}</Text>
          </Collapsible>
        </Box>
      )}
    </Card>
  );
}

function WebSearchResultView({ data }: { data: Record<string, unknown> }) {
  const results = asArray(data.results).map(asRecord);
  return (
    <FieldList>
      {asString(data.query) && <InlineField label="Query">{asString(data.query)}</InlineField>}
      <Collapsible title="Results" count={results.length} defaultOpen>
        <Flex direction="column" gap={1.5}>
          {results.length === 0 ? (
            <EmptyHint>No results</EmptyHint>
          ) : (
            results.map((result, index) => <WebResultCard key={index} result={result} />)
          )}
        </Flex>
      </Collapsible>
    </FieldList>
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

export function ToolResultView({ content }: { name: string; content: string }) {
  const parsed = tryParse(content);

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const data = parsed as Record<string, unknown>;
    const code = asString(data.code);
    if (code === "web_search_completed") return <WebSearchResultView data={data} />;
    if (code === "web_search_error") return <ErrorView message={asString(data.message) || "Search failed"} />;
    if (code.startsWith("bash")) return <BashResultView data={data} />;
    if (asString(data.kind) === "task") return <AgentTaskResultView data={data} />;
    if (code === "empty_response") return <EmptyHint>{asString(data.message) || "No output"}</EmptyHint>;
    return <GenericView data={data} />;
  }

  // Non-JSON results (e.g. a spawned agent's final text) render as prose.
  return <MarkdownContent content={content} />;
}
