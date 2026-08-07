"use client";

import { PERMISSION_MODES } from "@shared/controls";
import { useAgentName } from "@/lib/agent-names";
import { Alert, Box, Button, Flex, Link, Text } from "@chakra-ui/react";
import { useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { LuExternalLink } from "react-icons/lu";
import { openAccessibilitySettings, openBrowserRemoteDebugging } from "@/lib/api";
import { MarkdownContent } from "../markdown-content";
import { RelativeTime } from "../ui/relative-time";
import { Card, EmptyHint, Field, FieldList, InlineField, Mono, MonoBlock, MonoList, ProseList } from "../ui/display";
import { asArray, asRecord, asString } from "@/lib/coerce";
import { declaredNonMutating, requestedAccess } from "@shared/tools";
import { Pill } from "../ui/pill";
import { STATUS_PALETTE, taskLifecycleKind } from "@/lib/status";
import { hasBackgroundJobId, type ToolEventStatus } from "@/lib/tool-event";

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
  // The command as written, with nothing edited out of it on the way to the screen.
  const command = asString(args.command);
  const readOnly = declaredNonMutating(args);
  const access = requestedAccess(args);
  return (
    <FieldList>
      <Field label={translation("command")}>
        <MonoBlock>{command}</MonoBlock>
      </Field>
      <InlineField label={translation("readOnly")}>{readOnly ? translation("yes") : translation("no")}</InlineField>
      {access.writes.length > 0 && (
        <Field label={translation("accessWrite")}>
          <MonoList items={access.writes} />
        </Field>
      )}
      {access.reads.length > 0 && (
        <Field label={translation("accessRead")}>
          <MonoList items={access.reads} />
        </Field>
      )}
      {access.network && (
        <InlineField label={translation("accessNetwork")}>{translation("yes")}</InlineField>
      )}
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

// A task's status mapped to a translation key and a colour, so it reads as a badge rather than a raw lowercase value.
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

// One row shared by task creation and updates: number, status badge, prose as markdown, and dependency chips.
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

// Fields whose values are human prose, rendered as markdown in the normal font rather than monospace.
const PROSE_FIELD_KEYS = new Set([
  "explanation",
  "goal",
  // A goal is a sentence somebody wrote, and monospace made it read as an identifier.
  "previous_goal",
  "blocker",
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

/** The goal tool's outcome codes as words, read only by the result view. */
const GOAL_OUTCOME_KEYS: Record<string, string> = {
  goal_active: "goalActive",
  goal_satisfied: "goalSatisfied",
  goal_blocked: "goalBlocked",
  goal_cleared: "goalCleared",
};

// Translation keys for raw argument and result labels, falling back to the raw key when unmapped.
const FIELD_LABEL_KEYS: Record<string, string> = {
  // The goal tool, whose fields would otherwise fall through and be labelled with their own raw keys.
  status: "fieldStatus",
  goal: "goal",
  previous_goal: "previousGoal",
  requirements: "goalRequirements",
  evidence: "goalEvidence",
  blocker: "goalBlocker",
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
  access_request: "accessRequested",
  explanation: "explanation",
  uri: "fieldUri",
  query: "query",
  result_count: "results",
  job_id: "turnId",
  question: "question",
  // `status` is the call's lifecycle and `code` is what the tool decided, so they are different labels.
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

// `control_screen` reports its value or stdout, or an error, with missing grants rendered as their fix-it flow.
function ControlScreenResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  if (data.ok === false) {
    if (asString(data.code) === "browser_remote_debugging_off") {
      return <BrowserRemoteDebuggingAlert address={asString(data.enable_url)} />;
    }
    // Chrome is showing its own consent box, which is a state to wait in rather than a failure to route around.
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
  // What each action changed rather than what it was aimed at, which is the question a script cannot already answer.
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
  // Answers arrive as a per-question array of labels, flattened into pills rather than dumped as raw JSON.
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

// The peer-session tools, which are the most consequential calls a session makes and so get their own views.

function CreateSessionCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const agentName = useAgentName();
  const permissions = useTranslations("SessionControls");
  const message = asString(args.message);
  // The mode as the rest of the interface names it, rather than the wire's own spelling.
  const choice = PERMISSION_MODES.choices.find((item) => item.value === asString(args.permission_mode));
  const permissionKey = choice?.nameKey ?? choice?.labelKey;
  return (
    <FieldList>
      <InlineField label={translation("peerAgent")}>{agentName(asString(args.agent))}</InlineField>
      {permissionKey && (
        <InlineField label={translation("peerMode")}>
          {permissions(permissionKey as Parameters<typeof permissions>[0])}
        </InlineField>
      )}
      {asString(args.working_directory) && (
        <InlineField label={translation("peerDirectory")}>
          <Mono>{asString(args.working_directory)}</Mono>
        </InlineField>
      )}
      {message && (
        <Field label={translation("peerBrief")}>
          <MarkdownContent content={message} fontSize="xs" />
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
          <MarkdownContent content={message} fontSize="xs" />
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

// A peer's activity, named the way the sidebar names it, in every language the interface has.
const ACTIVITY_LABEL_KEYS: Record<string, string> = {
  working: "statusWorking",
  waiting: "awaitingInput",
  idle: "statusIdle",
  asleep: "statusAsleep",
  ended: "statusEnded",
};

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
  const agentName = useAgentName();
  const sessions = asArray(data.sessions).map(asRecord);
  if (sessions.length === 0) return <EmptyHint>{translation("noPeerSessions")}</EmptyHint>;
  return (
    <FieldList>
      {sessions.map((session, index) => (
        <InlineField key={index} label={agentName(asString(session.agent))}>
          <Flex align="center" gap={1.5} wrap="wrap">
            <Mono>{asString(session.id)}</Mono>
            {/* `activity` is what a peer is doing right now, derived by the daemon on every read. */}
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
  const agentName = useAgentName();
  const sessions = useTranslations("SessionsSidebar");
  const permissions = useTranslations("SessionControls");
  // The session id is not repeated here, since the call above already states which session this is about.
  const activity = asString(data.activity);
  const activityKey = ACTIVITY_LABEL_KEYS[activity];
  const resultChoice = PERMISSION_MODES.choices.find((item) => item.value === asString(data.permission_mode));
  const permissionKey = resultChoice?.nameKey ?? resultChoice?.labelKey;
  // The row's completed state already says the call worked, so only a status that is not `ok` is worth a line.
  const failed = asString(data.status) === "error";
  return (
    <FieldList>
      {asString(data.agent) && <InlineField label={translation("peerAgent")}>{agentName(asString(data.agent))}</InlineField>}
      {permissionKey && (
        <InlineField label={translation("peerMode")}>
          {permissions(permissionKey as Parameters<typeof permissions>[0])}
        </InlineField>
      )}
      {activityKey && (
        <InlineField label={translation("fieldStatus")}>
          <Pill colorPalette={PEER_ACTIVITY_PALETTE[activity] ?? "gray"}>
            {sessions(activityKey as Parameters<typeof sessions>[0])}
          </Pill>
        </InlineField>
      )}
      {failed && (
        <InlineField label={translation("fieldStatus")}>
          <Pill colorPalette="red">{translation("statusFailed")}</Pill>
        </InlineField>
      )}
    </FieldList>
  );
}

/** `update_goal`, whose call and result each state the whole thing, so neither repeats the other. */
/** A goal's requirements or evidence: prose lines, each its own row. */
function GoalLines({ label, lines }: { label: string; lines: string[] }) {
  if (!lines.length) return null;
  return (
    <Field label={label}>
      <ProseList items={lines} />
    </Field>
  );
}

function UpdateGoalCallView({ args }: { args: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const goal = asString(args.goal).trim();
  // The requirements are the half of a goal that says what done means, so a call that sets one shows both.
  const requirements = asArray(args.requirements).map(asString).filter(Boolean);
  if (!goal && !requirements.length) return null;
  return (
    <FieldList>
      {goal ? (
        <Field label={translation("goal")}>
          <MarkdownContent content={goal} fontSize="xs" />
        </Field>
      ) : null}
      <GoalLines label={translation("goalRequirements")} lines={requirements} />
    </FieldList>
  );
}

/** What the goal is now, and what it was. One outcome, stated once. */
// `wait_for` is entirely its own duration, which the heading already states, so its arguments render as nothing.
function WaitForResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolDisplay");
  const seconds = Number(data.seconds);
  const interrupted = asString(data.code) === "interrupted";
  if (interrupted) return <EmptyHint>{translation("waitInterrupted")}</EmptyHint>;
  if (!Number.isFinite(seconds)) return null;
  return <EmptyHint>{translation("waited", { seconds })}</EmptyHint>;
}

function UpdateGoalResultView({ data }: { data: Record<string, unknown> }) {
  const translation = useTranslations("ToolViews");
  const code = asString(data.code);
  const outcome = GOAL_OUTCOME_KEYS[code];
  const goal = asString(data.goal).trim();
  const previous = asString(data.previous_goal).trim();
  const requirements = asArray(data.requirements).map(asString).filter(Boolean);
  // What was checked when a goal was satisfied, so a reader can disagree with a done rather than take it on trust.
  const evidence = asArray(data.evidence).map(asString).filter(Boolean);
  const blocker = asString(data.blocker).trim();
  if (!outcome) return <ErrorView message={asString(data.message) || code} />;
  return (
    <FieldList>
      <InlineField label={translation("fieldOutcome")}>
        <Pill colorPalette={code === "goal_active" ? "blue" : code === "goal_blocked" ? "orange" : "green"}>
          {translation(outcome as Parameters<typeof translation>[0])}
        </Pill>
      </InlineField>
      {goal ? (
        <Field label={translation("goal")}><MarkdownContent content={goal} fontSize="xs" /></Field>
      ) : null}
      {previous ? (
        <Field label={translation("previousGoal")}><MarkdownContent content={previous} fontSize="xs" /></Field>
      ) : null}
      <GoalLines label={translation("goalRequirements")} lines={requirements} />
      <GoalLines label={translation("goalEvidence")} lines={evidence} />
      {blocker ? (
        <Field label={translation("goalBlocker")}><MarkdownContent content={blocker} fontSize="xs" /></Field>
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
        return <SessionReferenceCallView args={args} />;
      case "update_goal":
        return <UpdateGoalCallView args={args} />;
      // Its only argument is the duration, and the heading is already that duration.
      case "wait_for":
        return null;
      default: {
        // The explanation is already the collapsed heading, so it is stripped from the expanded body.
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
  // When a result was published, as a labelled field in the locale's own words rather than a raw wire timestamp.
  const published = asString(result.published_date);
  const publishedAt = published ? new Date(published) : null;
  const publishedKnown = publishedAt !== null && !Number.isNaN(publishedAt.getTime());
  // A bare `2026-08-04` carries no time, so showing one would be inventing midnight.
  return (
    <Card>
      {/* A column, so each part of the result is a line and the ellipsis has a box to happen in. */}
      <Flex direction="column" gap={1} minW={0}>
        {url ? (
          // Sized to its own text rather than stretched across the card, so the link is not a click target over empty space.
          <Link href={url} target="_blank" rel="noopener noreferrer" colorPalette="blue" textStyle="fieldLabel" alignSelf="flex-start">
            {title}
          </Link>
        ) : (
          <Text textStyle="fieldLabel">{title}</Text>
        )}
        {url && (
          <Mono color="fg.subtle" truncate>{url}</Mono>
        )}
        {published && (
          <InlineField label={translation("published")}>
            {publishedKnown
              ? <RelativeTime date={publishedAt} />
              // Whatever the provider sent, unparsed rather than dropped, since a date we cannot read is one the reader might.
              : published}
          </InlineField>
        )}
        {summary && (
          <Box color="fg.muted">
            <MarkdownContent content={summary} fontSize="xs" />
          </Box>
        )}
      </Flex>
    </Card>
  );
}

// The query and count are already on the call card, so the result renders only the result cards, unnested.
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

// One shared in-chat alert surface, whose palette drives the tint while callers supply the body.
function AlertBox({ colorPalette, children }: { colorPalette: string; children: ReactNode }) {
  return (
    <Box bg={`${colorPalette}.subtle`} border="1px solid" borderColor={`${colorPalette}.muted`} borderRadius="md" px={2.5} py={2}>
      {children}
    </Box>
  );
}

// Backend wording that describes a program rather than a person, replaced with a sentence naming the fix.
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

// Shown when the browser tool cannot reach Chrome: a brief message, the address, and a one-click button.
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

// Shown when a tool needs the macOS Accessibility grant, in the same in-chat alert language.
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

  // Discovery results are internal noise, since the call card already conveys that discovery happened.
  if (name === "list_mcp_tools" || name === "list_mcp_resources") return null;

  // The task tools confirm with raw ids the call card already shows as numbers, so the confirmation is dropped.
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
    if (name === "control_screen") return <ControlScreenResultView data={data} />;
    if (name === "fetch_url") return <FetchUrlResultView data={data} />;
    if (name === "load_skill") return <LoadSkillResultView data={data} />;
    if (name === "ask_user") return <AskUserResultView data={data} />;
    if (name === "list_sessions") return <SessionListResultView data={data} />;
    if (name === "create_session" || name === "read_session") {
      return <SessionResultView data={data} />;
    }
    // `message_session` reports only that it was accepted; the reply arrives as its own message in the transcript.
    if (name === "message_session") return null;
    if (name === "update_goal") return <UpdateGoalResultView data={data} />;
    if (name === "wait_for") return <WaitForResultView data={data} />;
    return <GenericView data={data} />;
  }

  // Non-JSON results (a tool that answers in prose rather than a payload) render as markdown.
  return <MarkdownContent content={content} />;
}
