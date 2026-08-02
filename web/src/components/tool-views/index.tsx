"use client";

import { Alert, Box, Button, Flex, IconButton, Image, Link, Text, Textarea } from "@chakra-ui/react";
import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode, type WheelEvent as ReactWheelEvent } from "react";
import { useTranslations } from "next-intl";
import { LuAppWindow, LuCheck, LuExternalLink, LuImageOff, LuRotateCw, LuTrash2 } from "react-icons/lu";
import { openAccessibilitySettings, openBrowserRemoteDebugging } from "@/lib/api";
import { MarkdownContent } from "../markdown-content";
import { Tooltip } from "../ui/tooltip";
import { Frame } from "../ui/semantic";
import { CenteredNumber } from "../ui/centered-number";
import { PanelEmptyState } from "../ui/panel";
import {
  Card,
  EmptyHint,
  Field,
  FieldList,
  InlineField,
  Mono,
  MonoBlock,
} from "../ui/display";
import { asArray, asRecord, asString } from "@/lib/coerce";
import { Pill } from "../ui/pill";
import { STATUS_PALETTE, taskLifecycleKind } from "@/lib/status";
import { hasBackgroundJobId, type ToolEventStatus } from "@/lib/tool-event";

function stripCdPrefix(command: string): string {
  const match = command.match(/^cd\s+'[^']*'\s+&&\s+(.*)/s);
  return match ? match[1] : command;
}

function tryParse(content: string): unknown {
  try {
    return JSON.parse(content);
  } catch {
    // Not JSON. Returning null is this function's answer, not a failure.
    return null;
  }
}

// Tool call (input) views

function BashCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const command = stripCdPrefix(asString(args.command));
  const readOnly = args.read_only !== false;
  const risk = asString(args.risk) || "low";
  // Display label for each bash risk level. Falls back to the raw value when
  // unmapped so an unexpected level still renders something readable.
  const riskLabels: Record<string, string> = {
    low: translation("riskLow"),
    medium: translation("riskMedium"),
    high: translation("riskHigh"),
  };
  const riskText = riskLabels[risk] ?? risk;
  return (
    <FieldList>
      <Field label={translation("command")}>
        <MonoBlock>{command}</MonoBlock>
      </Field>
      <InlineField label={translation("readOnly")}>{readOnly ? translation("yes") : translation("no")}</InlineField>
      <InlineField label={translation("risk")}>{riskText}</InlineField>
    </FieldList>
  );
}

function SearchWebCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <Field label={translation("query")}>
        <Text fontSize="xs">{asString(args.query)}</Text>
      </Field>
      {args.result_count != null && (
        <InlineField label={translation("results")}>{asString(args.result_count)}</InlineField>
      )}
    </FieldList>
  );
}

function SearchCodeCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <Field label={translation("query")}>
        <Text fontSize="xs">{asString(args.query)}</Text>
      </Field>
    </FieldList>
  );
}

// control_screen runs a script against a surface; the script is the substance, so it
// gets a full code block, with the surface as a compact field above it.
function ControlScreenCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      {asString(args.target) && (
        <InlineField label={translation("controlTarget")}><Mono>{asString(args.target)}</Mono></InlineField>
      )}
      <Field label={translation("controlScript")}>
        <MonoBlock>{asString(args.script)}</MonoBlock>
      </Field>
    </FieldList>
  );
}

// A task's lifecycle status → a translation key and a colour, so it reads as a
// proper badge instead of the raw lowercase value the model emits. The colour comes
// from the shared status palette (via the normalized kind); only the label key is
// task-list-specific. A completed task carries no badge (its settled row speaks).
const TASK_STATUS_LABEL_KEY: Record<string, string> = {
  running: "statusInProgress",
  blocked: "statusBlocked",
  canceled: "statusCancelled",
  failed: "statusDeleted",
  pending: "statusPending",
  unknown: "statusUnknown",
};

function taskStatusAppearance(status: string): { key: string; palette: string } | null {
  const kind = taskLifecycleKind(status);
  if (kind === "completed") return null;
  return { key: TASK_STATUS_LABEL_KEY[kind] ?? "statusUnknown", palette: STATUS_PALETTE[kind] };
}

// "task-..." -> "#..." — the internal id is never shown to the user, only its suffix.
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
  const translation = useTranslations("ToolViews");
  const appearance = taskStatusAppearance(status);
  return (
    <Card>
      <Flex align="center" gap={2} mb={body ? 1.5 : 0}>
        <Text textStyle="sectionLabel" flexShrink={0}>{label}</Text>
        <Box flex={1} />
        {appearance && <Pill colorPalette={appearance.palette}>{translation(appearance.key as Parameters<typeof translation>[0])}</Pill>}
      </Flex>
      {body && <MarkdownContent content={body} fontSize="xs" />}
      {dependencies.length > 0 && (
        <Flex align="center" gap={1} mt={1.5} flexWrap="wrap">
          <Text fontSize="2xs" color="fg.subtle">{translation("dependsOn")}</Text>
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
          body=""
        />
      ))}
    </FieldList>
  );
}

// Fields whose values are human prose (not identifiers/data) — rendered with the
// markdown renderer in the normal font rather than monospace.
const PROSE_FIELD_KEYS = new Set([
  "explanation",
  "goal",
  // A goal is a sentence somebody wrote, whether it is the current one or the one just
  // finished — monospace made the second read as an identifier.
  "previous_goal",
  "message",
  "prompt",
  "reason",
  "summary",
  "message",
  "content",
  "instructions",
  "query",
  "question",
  "response",
]);

/**
 * The goal tool's outcome codes, as words.
 *
 * `goal_satisfied` is a symbol from `dispatch.py`; "Satisfied" is what happened. Read by the
 * result view below, which is the only place a goal outcome is shown — the call view
 * deliberately does not repeat it.
 */
const GOAL_OUTCOME_KEYS: Record<string, string> = {
  goal_active: "goalActive",
  goal_satisfied: "goalSatisfied",
  goal_cleared: "goalCleared",
};

// Translation keys for raw argument/result key labels. Falls back to the raw key
// if unmapped. The actual label text is resolved through the ToolViews namespace.
const FIELD_LABEL_KEYS: Record<string, string> = {
  // The goal tool, whose fields fell through to this list unmapped and so were labelled with
  // their own raw keys — `status`, `previous_goal` — beside ones that had been translated.
  status: "fieldStatus",
  goal: "goal",
  previous_goal: "previousGoal",
  message: "message",
  error: "error",
  result: "result",
  matched: "matched",
  targets: "targets",
  tasks: "tasks",
  violation: "violation",
  current: "current",
  ok: "ok",
  turn_id: "turnId",
  server: "fieldServer",
  tool_name: "fieldToolName",
  arguments: "fieldArguments",
  read_only: "readOnly",
  explanation: "explanation",
  risk: "risk",
  uri: "fieldUri",
  query: "query",
  result_count: "results",
  job_id: "turnId",
  question: "question",
  // Not `fieldStatus`, which is what it used to be and is what made two rows of a goal call
  // both read "Status". They are different facts: `status` is the call's lifecycle — ok, error,
  // running — and `code` is what the tool decided. `tool_status_from_result` in
  // `protocol/events.py` depends on `status` meaning the first of those, so neither is
  // redundant; they were only named as though they were.
  code: "fieldOutcome",
  // file / search tools (arguments)
  file_path: "filePath",
  offset: "offset",
  limit: "limit",
  pattern: "pattern",
  include: "include",
  path: "path",
  start_line: "startLine",
  end_line: "endLine",
  new_lines: "newLines",
  content: "content",
  url: "url",
  format: "format",
  timeout: "timeout",
  name: "fieldName",
  questions: "fieldQuestions",
  options: "fieldOptions",
  header: "fieldHeader",
  multiple: "fieldMultiple",
  custom: "fieldCustom",
  // file / search tools (results)
  created: "created",
  characters: "characters",
  count: "count",
  matches: "matches",
  entries: "fieldEntries",
  truncated: "truncated",
  total_lines: "fieldTotalLines",
  sha256: "fieldSha256",
  title: "title",
  answers: "answers",
};

function ReadFileCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={translation("filePath")}>
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      {args.offset != null && <InlineField label={translation("offset")}>{asString(args.offset)}</InlineField>}
      {args.limit != null && <InlineField label={translation("limit")}>{asString(args.limit)}</InlineField>}
    </FieldList>
  );
}

function EditFileCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={translation("filePath")}>
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
    </FieldList>
  );
}

function WriteFileCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={translation("filePath")}>
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      <Field label={translation("content")}>
        <MonoBlock>{asString(args.content)}</MonoBlock>
      </Field>
    </FieldList>
  );
}

function FetchUrlCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={translation("url")}>
        <Mono>{asString(args.url)}</Mono>
      </InlineField>
      {args.format ? <InlineField label={translation("format")}>{asString(args.format)}</InlineField> : null}
      {args.timeout != null && <InlineField label={translation("timeout")}>{translation("secondsValue", { value: asString(args.timeout) })}</InlineField>}
    </FieldList>
  );
}

function LoadSkillCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={translation("skill")}>
        <Mono>{asString(args.name)}</Mono>
      </InlineField>
    </FieldList>
  );
}

function AskUserCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const questions = asArray(args.questions).map(asRecord);
  if (questions.length === 0) return null;
  return (
    <FieldList>
      {questions.map((item, index) => {
        const options = asArray(item.options).map(asRecord);
        const label = asString(item.header) || translation("questionN", { number: index + 1 });
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
                {translation("multiSelect")}
              </Text>
            ) : null}
          </Field>
        );
      })}
    </FieldList>
  );
}

function ReadFileResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  // The call already shows the file path; the result only confirms how much was read
  // (the line range) — the file body itself is the model's to read, not the transcript's.
  const range = [asString(data.start_line), asString(data.end_line)].filter(Boolean).join("–");
  const total = asString(data.total_lines);
  if (!range) return null;
  return (
    <FieldList>
      <InlineField label={translation("lines")}>
        {total ? translation("linesOfTotal", { range, total }) : range}
      </InlineField>
    </FieldList>
  );
}

// One code match: file:line-range heading (with language + score muted alongside)
// over the matched snippet, rendered like a small diff/file body.
function CodeMatchCard({ match }: { match: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const file = asString(match.file);
  const range = [asString(match.start_line), asString(match.end_line)].filter(Boolean).join("–");
  const language = asString(match.language);
  const score = asString(match.score);
  const snippet = asString(match.snippet);
  return (
    <Card>
      <Flex align="center" gap={2}>
        <Mono flex={1} minW={0} truncate>{range ? `${file}:${range}` : file}</Mono>
        {language && <Text fontSize="2xs" color="fg.subtle" flexShrink={0}>{language}</Text>}
        {score && <Text fontSize="2xs" color="fg.subtle" flexShrink={0}>{translation("searchScore")} {score}</Text>}
      </Flex>
      {snippet && (
        <Box mt={1}>
          <MonoBlock>{snippet}</MonoBlock>
        </Box>
      )}
    </Card>
  );
}

// The query is already on the call card, so the result only surfaces the match count
// and the matched snippets as cards.
function SearchCodeResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const matches = asArray(data.matches).map(asRecord);
  if (matches.length === 0) return <EmptyHint>{translation("noResults")}</EmptyHint>;
  return (
    <FieldList>
      <InlineField label={translation("count")}>{asString(data.count) || String(matches.length)}</InlineField>
      <Field label={translation("matches")}>
        <Flex direction="column" gap={1.5}>
          {matches.map((match, index) => <CodeMatchCard key={index} match={match} />)}
        </Flex>
      </Field>
    </FieldList>
  );
}

// One line describing what an action changed: where it landed, then what moved. A record that
// changed nothing says so — that is the whole point of reporting changes rather than intentions.
// What one action changed, as elements rather than as a sentence assembled in TypeScript.
//
// This was a string built with `·` and `—` glue and a `.replace("{count}", …)` done by hand — which
// is not a translation, it is English word order and Latin typography shipped to every locale, with
// the plural rule silently assumed to be English's. Layout separates the parts now, each part is
// its own message, and the count goes through ICU so a locale decides its own plural.
function ChangeRow({ entry }: { entry: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const where = asString(entry.name) || asString(entry.role) || asString(entry.id);
  const navigated = asRecord(entry.navigated);
  const destination = asString(navigated.title) || asString(navigated.url);
  const appearedCount = Number(entry.appeared_total) || asArray(entry.appeared).length;
  const nothingChanged = Array.isArray(entry.changed) && entry.changed.length === 0;
  return (
    <Flex align="baseline" gap={2} wrap="wrap">
      <Text fontSize="2xs" color="fg.muted">{asString(entry.action)}</Text>
      {where && <Mono fontSize="2xs" color="fg.subtle">{where}</Mono>}
      {destination && <Text fontSize="2xs" color="fg.muted">{destination}</Text>}
      {appearedCount > 0 && (
        <Text fontSize="2xs" color="fg.subtle">{translation("controlAppeared", { count: appearedCount })}</Text>
      )}
      {nothingChanged && <Text fontSize="2xs" color="fg.subtle">{translation("controlNoChange")}</Text>}
      {entry.visible === false && <Text fontSize="2xs" color="fg.subtle">{translation("controlOffScreen")}</Text>}
    </Flex>
  );
}

// control_screen runs a script and reports its value / stdout, or an error with
// an optional traceback. Debugging-off / missing grants render as their fix-it flow.
function ControlScreenResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  if (data.ok === false) {
    if (asString(data.code) === "browser_remote_debugging_off") {
      return <BrowserRemoteDebuggingAlert address={asString(data.enable_url)} />;
    }
    // Chrome is showing its own consent box and nobody has answered it yet. This is a state to
    // wait in, not a failure to route around — and emphatically not one to "fix" by toggling
    // remote debugging, which dismisses the very prompt being waited on.
    if (asString(data.awaiting) === "browser_authorization") {
      return <BrowserAuthorizationPending />;
    }
    if (asString(data.needs_permission)) return <PermissionGrantAlert />;
    const traceback = asString(data.traceback);
    return (
      <FieldList>
        <ErrorView message={asString(data.error) || translation("failed")} />
        {traceback && (
          <Field label={translation("controlTraceback")}>
            <MonoBlock>{traceback}</MonoBlock>
          </Field>
        )}
      </FieldList>
    );
  }
  const resultValue = data.value;
  const resultText = resultValue == null ? "" : typeof resultValue === "object" ? JSON.stringify(resultValue, null, 2) : asString(resultValue);
  const stdout = asString(data.stdout);
  // What each action *changed*, not what it was aimed at. `acted_on` answered the one question a
  // script already knew the answer to; this answers whether anything happened.
  const changed = asArray(data.changed).map(asRecord);
  if (!resultText && !stdout && changed.length === 0) return null;
  return (
    <FieldList>
      {changed.length > 0 && (
        <Field label={translation("controlChanged")}>
          <Flex direction="column" gap={1}>
            {changed.map((entry, index) => <ChangeRow key={index} entry={entry} />)}
          </Flex>
        </Field>
      )}
      {resultText && (
        <Field label={translation("controlReturn")}>
          <MonoBlock>{resultText}</MonoBlock>
        </Field>
      )}
      {stdout && (
        <Field label={translation("output")}>
          <MonoBlock>{stdout}</MonoBlock>
        </Field>
      )}
    </FieldList>
  );
}

function FileEditResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  // Shared by edit_file and write_file. Path is on the call card.
  const code = asString(data.code);
  const diagnostic = asRecord(data.diagnostic);
  const message = asString(data.message);

  if (code === "edit_find_not_found" || code === "edit_find_near_miss" || code === "edit_find_not_unique") {
    const occurrences = asString(data.occurrences);
    return (
      <FieldList>
        <InlineField label={translation("match")}>
          <Pill colorPalette="red">{code === "edit_find_not_unique" ? translation("notUnique") : translation("notFound")}</Pill>
        </InlineField>
        {code === "edit_find_not_unique" && occurrences && (
          <InlineField label={translation("occurrences")}>{occurrences}</InlineField>
        )}
        {message && (
          <Field label={translation("reason")}>
            <Text fontSize="xs" color="fg.subtle">{message}</Text>
          </Field>
        )}
      </FieldList>
    );
  }

  if (code === "edit_failed_validation" && diagnostic.origin) {
    const contextLines = asArray(diagnostic.context_snapshot).map(asString);
    return (
      <FieldList>
        <InlineField label={translation("validation")}>
          <Pill colorPalette="red">{translation("failed")}</Pill>
        </InlineField>
        <InlineField label={translation("origin")}>{asString(diagnostic.origin)}</InlineField>
        <InlineField label={translation("language")}>{asString(diagnostic.language)}</InlineField>
        {asString(diagnostic.line) && (
          <InlineField label={translation("line")}>{asString(diagnostic.line)}:{asString(diagnostic.column)}</InlineField>
        )}
        <InlineField label={translation("error")}>{asString(diagnostic.message)}</InlineField>
        {contextLines.length > 0 && (
          <Field label={translation("context")}>
            <MonoBlock maxH={32}>{contextLines.join("\n")}</MonoBlock>
          </Field>
        )}
        {message && (
          <Field label={translation("recovery")}>
            <Text fontSize="xs" color="fg.subtle">{message}</Text>
          </Field>
        )}
      </FieldList>
    );
  }

  // Successful edit details stay collapsed to the call row; the group-level +/-
  // counters retain the useful summary without rendering a full inline diff.
  return null;
}

function FetchUrlResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  // URL + format are on the call card; only surface truncation + fetched content.
  const content = asString(data.content);
  return (
    <FieldList>
      {data.truncated === true && <InlineField label={translation("truncated")}>{translation("yes")}</InlineField>}
      {content && (
        <Field label={translation("content")}>
          <MarkdownContent content={content} fontSize="xs" />
        </Field>
      )}
    </FieldList>
  );
}

function LoadSkillResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  // Skill name is on the call card; the internal resolved `path` is dropped.
  const title = asString(data.title);
  const content = asString(data.content);
  return (
    <FieldList>
      {title && <InlineField label={translation("title")}>{title}</InlineField>}
      {content && (
        <Field label={translation("content")}>
          <MarkdownContent content={content} fontSize="xs" />
        </Field>
      )}
    </FieldList>
  );
}

function AskUserResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  // Answers arrive as a per-question array of labels (string | string[]). Flatten
  // into readable pills instead of dumping the raw JSON array.
  const answers = asArray(data.answers);
  const labels: string[] = [];
  for (const answer of answers) {
    if (Array.isArray(answer)) labels.push(...answer.map(asString));
    else labels.push(asString(answer));
  }
  const shown = labels.filter(Boolean);
  if (shown.length === 0) return <EmptyHint>{translation("noAnswer")}</EmptyHint>;
  return (
    <FieldList>
      <Field label={translation("answers")}>
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
  const translation = useTranslations("ToolViews");
  const entries = Object.entries(data);
  if (entries.length === 0) return <EmptyHint>{translation("noData")}</EmptyHint>;
  return (
    <FieldList>
      {entries.map(([key, value]) => (
        <InlineField key={key} label={FIELD_LABEL_KEYS[key] ? translation(FIELD_LABEL_KEYS[key] as Parameters<typeof translation>[0]) : key}>
          {value && typeof value === "object" ? (
            // Structured values (objects/arrays) are data — monospace JSON.
            <MonoBlock>{JSON.stringify(value, null, 2)}</MonoBlock>
          ) : PROSE_FIELD_KEYS.has(key) ? (
            // Prose values render as markdown, sized to match the compact field context.
            <MarkdownContent content={asString(value)} fontSize="xs" />
          ) : (
            // Scalar identifiers/data (names, ids, flags) render in monospace.
            <Mono whiteSpace="pre-wrap">{asString(value)}</Mono>
          )}
        </InlineField>
      ))}
    </FieldList>
  );
}

// The peer-session tools.
//
// The most consequential calls a session makes, and until now the least legible: they fell
// through to the raw argument dump, so a `create_session` brief — often several paragraphs —
// landed in the transcript as an unformatted blob.

function CreateSessionCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const message = asString(args.message);
  return (
    <FieldList>
      <InlineField label={translation("peerAgent")}>{asString(args.agent)}</InlineField>
      {asString(args.permission_mode) && (
        <InlineField label={translation("peerMode")}>{asString(args.permission_mode)}</InlineField>
      )}
      {asString(args.working_directory) && (
        <InlineField label={translation("peerDirectory")}>
          <Mono>{asString(args.working_directory)}</Mono>
        </InlineField>
      )}
      {message && (
        <Field label={translation("peerBrief")}>
          <MarkdownContent content={message} />
        </Field>
      )}
    </FieldList>
  );
}

function MessageSessionCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const message = asString(args.message);
  return (
    <FieldList>
      <InlineField label={translation("peerSession")}>
        <Mono>{asString(args.session)}</Mono>
      </InlineField>
      {message && (
        <Field label={translation("message")}>
          <MarkdownContent content={message} />
        </Field>
      )}
    </FieldList>
  );
}

function SessionReferenceCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const session = asString(args.session);
  if (!session) return null;
  return (
    <FieldList>
      <InlineField label={translation("peerSession")}>
        <Mono>{session}</Mono>
      </InlineField>
    </FieldList>
  );
}

// A peer's activity, coloured the way the sidebar colours the same thing.
const PEER_ACTIVITY_PALETTE: Record<string, string> = {
  working: "blue",
  waiting: "orange",
  idle: "gray",
  asleep: "gray",
  ended: "gray",
};

// One row per session, so a listing reads as a list of peers rather than as nested JSON.
function SessionListResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const sessions = asArray(data.sessions).map(asRecord);
  if (sessions.length === 0) return <EmptyHint>{translation("noPeerSessions")}</EmptyHint>;
  return (
    <FieldList>
      {sessions.map((session, index) => (
        <InlineField key={index} label={asString(session.agent)}>
          <Flex align="center" gap={1.5} wrap="wrap">
            <Mono>{asString(session.id)}</Mono>
            {/* `activity` is what a peer is doing right now, derived by the daemon on every
                read: working, waiting on a person, idle, asleep (no process — the next
                message forks one), or ended. It replaced a `status` field that tried to be
                both this and whether the session still exists, and a separate `busy` that
                had to be merged in to answer "is it working". */}
            <Pill colorPalette={PEER_ACTIVITY_PALETTE[asString(session.activity)] ?? "gray"}>
              {asString(session.activity) || asString(session.lifecycle)}
            </Pill>
            {session.awaiting_input ? <Pill colorPalette="orange">{translation("peerWaiting")}</Pill> : null}
          </Flex>
        </InlineField>
      ))}
    </FieldList>
  );
}

function SessionResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={translation("peerSession")}>
        <Mono>{asString(data.session) || asString(data.id)}</Mono>
      </InlineField>
      {asString(data.agent) && <InlineField label={translation("peerAgent")}>{asString(data.agent)}</InlineField>}
      {asString(data.permission_mode) && (
        <InlineField label={translation("peerMode")}>{asString(data.permission_mode)}</InlineField>
      )}
      {asString(data.status) && (
        <InlineField label={translation("fieldStatus")}>
          <Pill colorPalette={STATUS_PALETTE[taskLifecycleKind(asString(data.status))]}>
            {asString(data.status)}
          </Pill>
        </InlineField>
      )}
    </FieldList>
  );
}

/**
 * `update_goal`, whose call and result each said the whole thing.
 *
 * The result's `code` is `f"goal_{status}"` in `dispatch.py` — derived from the argument, so a
 * generic dump of both rendered the same fact twice under two labels: "Status / Now working
 * toward this" from the call, "Outcome / Now working toward this" from the result. Relabelling
 * could not fix that; only one of them is worth showing.
 *
 * The result is the one that is true — a call is a request, and this one can be refused — so the
 * call shows only what the result cannot: the goal being proposed. Setting a goal states it here
 * and confirms it there; finishing one carries no argument worth rendering at all, since
 * `explanation` is already the heading.
 */
function UpdateGoalCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const goal = asString(args.goal).trim();
  if (!goal) return null;
  return (
    <FieldList>
      <Field label={translation("goal")}>
        <MarkdownContent content={goal} fontSize="xs" />
      </Field>
    </FieldList>
  );
}

/** What the goal is now, and what it was. One outcome, stated once. */
function UpdateGoalResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const code = asString(data.code);
  const outcome = GOAL_OUTCOME_KEYS[code];
  const goal = asString(data.goal).trim();
  const previous = asString(data.previous_goal).trim();
  if (!outcome) return <ErrorView message={asString(data.message) || code} />;
  return (
    <FieldList>
      <InlineField label={translation("fieldOutcome")}>
        <Pill colorPalette={code === "goal_active" ? "blue" : "green"}>
          {translation(outcome as Parameters<typeof translation>[0])}
        </Pill>
      </InlineField>
      {goal ? (
        <Field label={translation("goal")}><MarkdownContent content={goal} fontSize="xs" /></Field>
      ) : null}
      {previous ? (
        <Field label={translation("previousGoal")}><MarkdownContent content={previous} fontSize="xs" /></Field>
      ) : null}
    </FieldList>
  );
}

export function ToolCallView({ name, args }: { name: string; args?: Record<string, unknown> }) {
  if (!args) return null;
  const specificView = (() => {
    switch (name) {
      case "bash":
        return <BashCallView args={args} />;
      case "search_web":
        return <SearchWebCallView args={args} />;
      case "set_tasks":
        return <WriteTasksCallView args={args} />;
      case "update_tasks":
        return <UpdateTasksCallView args={args} />;
      case "read_file":
        return <ReadFileCallView args={args} />;
      case "edit_file":
        return <EditFileCallView args={args} />;
      case "write_file":
        return <WriteFileCallView args={args} />;
      case "search_code":
        return <SearchCodeCallView args={args} />;
      case "control_screen":
        return <ControlScreenCallView args={args} />;
      case "fetch_url":
        return <FetchUrlCallView args={args} />;
      case "load_skill":
        return <LoadSkillCallView args={args} />;
      case "ask_user":
        return <AskUserCallView args={args} />;
      case "create_session":
        return <CreateSessionCallView args={args} />;
      case "message_session":
        return <MessageSessionCallView args={args} />;
      case "read_session":
      case "end_session":
        return <SessionReferenceCallView args={args} />;
      case "update_goal":
        return <UpdateGoalCallView args={args} />;
      default: {
        // The explanation is already the collapsed heading (the tool-call title);
        // strip it so the expanded body never repeats it. MCP calls fall here too.
        const rest = { ...args };
        delete rest.explanation;
        return <GenericView data={rest} />;
      }
    }
  })();
  return specificView;
}

// Tool result (output) views

function BashResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const output = asString(data.output);
  const outputFile = asString(data.output_file);
  const hasMeta = data.pid != null || data.size != null;
  if (!output && !outputFile && !hasMeta) return null;
  return (
    <FieldList>
      {data.pid != null && <InlineField label={translation("pid")}>{asString(data.pid)}</InlineField>}
      {data.size != null && <InlineField label={translation("size")}>{translation("bytesValue", { value: asString(data.size) })}</InlineField>}
      {data.truncated === true && <InlineField label={translation("truncated")}>{translation("yes")}</InlineField>}
      {output ? (
        <Field label={translation("output")}>
          <MonoBlock>{output}</MonoBlock>
        </Field>
      ) : outputFile ? (
        <InlineField label={translation("output")}><Mono>{outputFile}</Mono></InlineField>
      ) : null}
      {output && outputFile ? <InlineField label={translation("fullOutput")}><Mono>{outputFile}</Mono></InlineField> : null}
    </FieldList>
  );
}

function WebResultCard({ result }: { result: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const title = asString(result.title) || translation("untitled");
  const url = asString(result.url);
  const summary = asString(result.summary);
  const date = asString(result.published_date);
  return (
    <Card>
      {url ? (
        <Link href={url} target="_blank" rel="noopener noreferrer" colorPalette="blue" textStyle="fieldLabel">
          {title}
        </Link>
      ) : (
        <Text textStyle="fieldLabel">{title}</Text>
      )}
      {url && (
        <Mono color="fg.subtle" truncate>{url}</Mono>
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
function SearchWebResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const results = asArray(data.results).map(asRecord);
  if (results.length === 0) return <EmptyHint>{translation("noResults")}</EmptyHint>;
  return (
    <Flex direction="column" gap={1.5}>
      {results.map((result, index) => <WebResultCard key={index} result={result} />)}
    </Flex>
  );
}

// One shared in-chat alert surface — a tinted, bordered box with unified padding.
// The palette drives the background/border tint (red for errors, yellow for the
// fixable permission/debugging prompts); callers supply the body.
function AlertBox({ colorPalette, children }: { colorPalette: string; children: ReactNode }) {
  return (
    <Box bg={`${colorPalette}.subtle`} border="1px solid" borderColor={`${colorPalette}.muted`} borderRadius="md" px={2.5} py={2}>
      {children}
    </Box>
  );
}

// Backend wording that says what happened to a program rather than to a person. Each is
// replaced with a sentence that names the failure and what can be done about it; anything
// unrecognised still shows verbatim, under a heading, because an unexplained error is worse
// than a blunt one.
const REPHRASED_ERRORS: ReadonlyArray<readonly [RegExp, string]> = [
  [/^control_screen: the script produced no result\.?$/i, "controlScreenNoResult"],
  [/^control_screen: the script process died before returning a result\.?$/i, "controlScreenDied"],
];

function ErrorView({ message }: { message: string }) {
  const translation = useTranslations("ToolViews");
  const trimmed = message.trim();
  const rephrased = REPHRASED_ERRORS.find(([pattern]) => pattern.test(trimmed));
  const body = rephrased ? translation(rephrased[1] as Parameters<typeof translation>[0]) : trimmed;
  return (
    <Alert.Root status="error" size="sm" borderRadius="md" alignItems="flex-start">
      <Alert.Indicator />
      <Alert.Content flex={1} minW={0}>
        <Alert.Title fontSize="xs">{translation("errorTitle")}</Alert.Title>
        <Alert.Description fontSize="xs" color="fg.muted">{body}</Alert.Description>
      </Alert.Content>
    </Alert.Root>
  );
}

// Shown when the browser tool can't reach Chrome because remote debugging is off: a brief message,
// the exact address, and a one-click button that opens that settings page in the user's browser.
// Chrome asks the user to approve a debugging connection, in the browser window rather than
// here, and until now nothing in this interface said so: attaching waited ten seconds and then
// reported a stale endpoint, advising a toggle that dismisses the prompt. Waiting is a state,
// and it is the user who ends it.
function BrowserAuthorizationPending() {
  const translation = useTranslations("ToolViews");
  return (
    <AlertBox colorPalette="blue">
      <Text textStyle="fieldLabel">{translation("browserAuthorizationTitle")}</Text>
      <Text fontSize="xs" color="fg.muted" mt={0.5}>{translation("browserAuthorizationBody")}</Text>
    </AlertBox>
  );
}

function BrowserRemoteDebuggingAlert({ address, browserName }: { address: string; browserName?: string }) {
  const translation = useTranslations("ToolViews");
  const [opened, setOpened] = useState(false);
  return (
    <AlertBox colorPalette="yellow">
      <Text textStyle="fieldLabel">{translation("browserEnableTitle")}</Text>
      <Text fontSize="xs" color="fg.muted" mt={0.5}>{translation("browserEnableBody")}</Text>
      <Flex align="center" gap={2} mt={2}>
        <Button
          size="xs"
          colorPalette="yellow"
          variant="solid"
          onClick={async () => setOpened(await openBrowserRemoteDebugging(browserName || "chrome"))}
        >
          <LuExternalLink size={12} />
          {translation("browserEnableButton")}
        </Button>
        <Mono fontSize="2xs" color="fg.subtle">{address}</Mono>
      </Flex>
      {opened && <Text fontSize="2xs" color="green.fg" mt={1.5}>{translation("browserEnableOpened")}</Text>}
    </AlertBox>
  );
}

// Shown when a tool needs the macOS Accessibility grant — the only grant a tool can be
// missing now. Same in-chat alert language as the remote-debugging one: a brief message
// and a one-click button that surfaces the system prompt and opens the right System
// Settings pane.
function PermissionGrantAlert() {
  const translation = useTranslations("ToolViews");
  const [opened, setOpened] = useState(false);
  return (
    <AlertBox colorPalette="yellow">
      <Text textStyle="fieldLabel">
        {translation("permissionAccessibilityTitle")}
      </Text>
      <Text fontSize="xs" color="fg.muted" mt={0.5}>
        {translation("permissionAccessibilityBody")}
      </Text>
      <Flex align="center" gap={2} mt={2}>
        <Button
          size="xs"
          colorPalette="yellow"
          variant="solid"
          onClick={async () => {
            await openAccessibilitySettings();
            setOpened(true);
          }}
        >
          <LuExternalLink size={12} />
          {translation("permissionGrantButton")}
        </Button>
      </Flex>
      {opened && <Text fontSize="2xs" color="green.fg" mt={1.5}>{translation("permissionOpened")}</Text>}
    </AlertBox>
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
    // Not JSON. Returning null is this function's answer, not a failure.
    return "";
  }
}

function safeImageSource(value: string): string {
  if (value.startsWith("data:image/")) return value;
  return safeWebUrl(value);
}

function clamp(value: number, lower: number, upper: number): number {
  return Math.min(upper, Math.max(lower, value));
}

// Zoom is relative to the fitted scale (1 = the image exactly fits the container), so the
// floor is 1: you can never zoom out past "fits", which would shrink the image to nothing.
const MINIMUM_RELATIVE_IMAGE_ZOOM = 1;
const MAXIMUM_RELATIVE_IMAGE_ZOOM = 5;

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

function McpResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  if (data.is_error === true) {
    return <ErrorView message={translation("mcpToolError")} />;
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

export function ToolResultView({
  name,
  content,
  status,
}: {
  name: string;
  content: string;
  status?: ToolEventStatus;
}) {
  const translation = useTranslations("ToolViews");
  const parsed = tryParse(content);

  // MCP discovery results (the full server/tool catalog with JSON schemas) are
  // internal noise — the call card already conveys that discovery happened.
  if (name === "list_mcp_tools" || name === "list_mcp_resources") return null;

  // The task tools' result is a bare confirmation string that names raw "task-..."
  // ids (for the model); the call card already shows the tasks as #..., so don't
  // re-render the confirmation and leak the internal ids.
  if (name === "set_tasks" || name === "update_tasks") return null;

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const data = parsed as Record<string, unknown>;
    const code = asString(data.code);
    if (status === "running" && hasBackgroundJobId(data)) return null;
    if (code === "tool_error") return null;
    if (code === "web_search_completed") return <SearchWebResultView data={data} />;
    if (code === "web_search_error") return <ErrorView message={asString(data.message) || translation("searchFailed")} />;
    if (code.startsWith("bash")) return <BashResultView data={data} />;
    if (name === "call_mcp_tool" || name === "read_mcp_resource") return <McpResultView data={data} />;
    if (code === "empty_response") {
      const message = asString(data.message);
      return message ? <EmptyHint>{message}</EmptyHint> : null;
    }
    if (name === "read_file") return <ReadFileResultView data={data} />;
    if (name === "search_code") return <SearchCodeResultView data={data} />;
    if (name === "control_screen") return <ControlScreenResultView data={data} />;
    if (name === "edit_file" || name === "write_file") return <FileEditResultView data={data} />;
    if (name === "fetch_url") return <FetchUrlResultView data={data} />;
    if (name === "load_skill") return <LoadSkillResultView data={data} />;
    if (name === "ask_user") return <AskUserResultView data={data} />;
    if (name === "list_sessions") return <SessionListResultView data={data} />;
    if (name === "create_session" || name === "read_session" || name === "end_session") {
      return <SessionResultView data={data} />;
    }
    // `message_session` reports only that it was accepted. There is nothing to show: the reply,
    // when there is one, arrives as its own message in the transcript.
    if (name === "message_session") return null;
    if (name === "update_goal") return <UpdateGoalResultView data={data} />;
    return <GenericView data={data} />;
  }

  // Non-JSON results (a tool that answers in prose rather than a payload) render as markdown.
  return <MarkdownContent content={content} />;
}
