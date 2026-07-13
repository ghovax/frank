"use client";

import { Box, Button, Flex, IconButton, Link, Text, Textarea } from "@chakra-ui/react";
import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode, type WheelEvent as ReactWheelEvent } from "react";
import { useTranslations } from "next-intl";
import { LuAppWindow, LuCheck, LuExternalLink, LuImageOff, LuRotateCw, LuTrash2 } from "react-icons/lu";
import ReactDiffViewer, { DiffMethod } from "react-diff-viewer-continued";
import { useColorMode } from "../ui/color-mode";
import { type A2ATask, taskArtifactText } from "@/lib/use-chat";
import { artifactPageUrl, artifactProxyUrl, openAccessibilitySettings, openBrowserRemoteDebugging, openScreenRecordingSettings } from "@/lib/api";
import { imageIdentityForArtifact, type ArtifactImageAnnotation, type ArtifactImageIdentity } from "@/lib/artifact-annotations";
import { useArtifactEvent } from "../artifact-bridge";
import { MarkdownContent } from "../markdown-content";
import { Tooltip } from "../ui/tooltip";
import { CenteredNumber } from "../ui/centered-number";
import { PanelEmptyState } from "../ui/panel";
import {
  asArray,
  asRecord,
  asString,
  Card,
  EmptyHint,
  Field,
  FieldList,
  InlineField,
  Mono,
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

function createClientIdentifier(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

// Tool call (input) views

function BashCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const command = stripCdPrefix(asString(args.command));
  const readOnly = args.read_only !== false;
  const risk = asString(args.risk) || "low";
  // Display label for each bash risk level. Falls back to the raw value when
  // unmapped so an unexpected level still renders something readable.
  const riskLabels: Record<string, string> = {
    low: t("riskLow"),
    medium: t("riskMedium"),
    high: t("riskHigh"),
  };
  const riskText = riskLabels[risk] ?? risk;
  return (
    <FieldList>
      <Field label={t("command")}>
        <MonoBlock>{command}</MonoBlock>
      </Field>
      <InlineField label={t("readOnly")}>{readOnly ? t("yes") : t("no")}</InlineField>
      <InlineField label={t("risk")}>{riskText}</InlineField>
    </FieldList>
  );
}

function WebSearchCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  return (
    <FieldList>
      <Field label={t("query")}>
        <Text fontSize="xs">{asString(args.query)}</Text>
      </Field>
      {args.result_count != null && (
        <InlineField label={t("results")}>{asString(args.result_count)}</InlineField>
      )}
    </FieldList>
  );
}

function agentLabelFor(agentName: string, agents: { id: string; name: string; title?: string }[]): string {
  const agent = agents.find((candidate) => candidate.id === agentName);
  return agent?.title || agent?.name || agentName || "Agent";
}

function SpawnAgentCallView({ args, agents }: { args: Record<string, unknown>; agents: { id: string; name: string; title?: string }[] }) {
  const t = useTranslations("ToolViews");
  const agentName = asString(args.agent) || "assistant";
  return (
    <FieldList>
      <InlineField label={t("agent")}>
        <Pill colorPalette="purple">{agentLabelFor(agentName, agents)}</Pill>
      </InlineField>
      <Field label={t("prompt")}>
        <MarkdownContent content={asString(args.prompt)} fontSize="xs" />
      </Field>
    </FieldList>
  );
}


// Backend action name → a clear, human label key. The tool's raw actions are terse verbs
// ("observe", "run_script"); the badge maps them to what they actually do, the same way every
// other enum in these views is mapped rather than shown raw.
const COMPUTER_ACTION_LABEL_KEYS: Record<string, string> = {
  observe: "computerActionObserve",
  click: "computerActionClick",
  type: "computerActionType",
  key: "computerActionKey",
  menu: "computerActionMenu",
  scroll: "computerActionScroll",
  screenshot: "computerActionScreenshot",
  launch: "computerActionLaunch",
  run_script: "computerActionRunScript",
};

// The mapped action as a pill. Shown both on the tool-call heading (so the action reads at a
// glance) and inside the expanded body. Exported for the heading in tool-call.tsx.
export function ComputerActionBadge({ action }: { action?: string }) {
  const t = useTranslations("ToolViews");
  if (!action) return null;
  const key = COMPUTER_ACTION_LABEL_KEYS[action];
  return <Pill colorPalette="cyan">{key ? t(key as Parameters<typeof t>[0]) : action}</Pill>;
}

// The computer-control tool packs one of several actions into a shared argument set,
// so only the fields that action actually uses are shown — the action itself as a pill,
// then whichever of app / element / key combo / menu path / direction / etc. are present.
function ComputerCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const action = asString(args.action);
  const modifiers = asArray(args.modifiers).map(asString).filter(Boolean);
  const keyName = asString(args.key);
  const keyCombo = [...modifiers, keyName].filter(Boolean).join(" + ");
  const menuPath = asArray(args.menu_path).map(asString).filter(Boolean).join(" ▸ ");
  const launchArguments = asArray(args.arguments).join(" ");
  const text = asString(args.text);
  const windowScope = asString(args.window);
  const clicks = Number(args.clicks ?? 1);
  const button = asString(args.button);
  const isScript = action === "run_script";
  return (
    <FieldList>
      <InlineField label={t("computerAction")}>
        <ComputerActionBadge action={action} />
      </InlineField>
      {asString(args.app) && <InlineField label={t("computerApp")}>{asString(args.app)}</InlineField>}
      {args.element != null && (
        <InlineField label={t("computerElement")}>
          <Mono>{asString(args.element)}</Mono>
        </InlineField>
      )}
      {keyCombo && (
        <InlineField label={t("computerKey")}>
          <Mono>{keyCombo}</Mono>
        </InlineField>
      )}
      {menuPath && <InlineField label={t("computerMenuPath")}>{menuPath}</InlineField>}
      {asString(args.direction) && <InlineField label={t("computerDirection")}>{asString(args.direction)}</InlineField>}
      {clicks > 1 && <InlineField label={t("computerClicks")}>{String(clicks)}</InlineField>}
      {button === "right" && <InlineField label={t("computerButton")}>{button}</InlineField>}
      {windowScope && windowScope !== "focused" && <InlineField label={t("computerWindow")}>{windowScope}</InlineField>}
      {launchArguments && <InlineField label={t("fieldArguments")}>{launchArguments}</InlineField>}
      {isScript && asString(args.language) && <InlineField label={t("language")}>{asString(args.language)}</InlineField>}
      {text && (
        isScript ? (
          <Field label={t("computerScript")}>
            <MonoBlock>{text}</MonoBlock>
          </Field>
        ) : (
          <Field label={t("computerText")}>
            <Text fontSize="xs" whiteSpace="pre-wrap">{text}</Text>
          </Field>
        )
      )}
    </FieldList>
  );
}

// The browser tool's actions mapped to clear labels, same idea as the computer tool.
const BROWSER_ACTION_LABEL_KEYS: Record<string, string> = {
  navigate: "browserActionNavigate",
  observe: "browserActionObserve",
  find: "browserActionFind",
  screenshot: "browserActionScreenshot",
  select: "browserActionSelect",
  upload: "browserActionUpload",
  drag: "browserActionDrag",
  click: "browserActionClick",
  type: "browserActionType",
  read: "browserActionRead",
  press: "browserActionPress",
  hover: "browserActionHover",
  scroll: "browserActionScroll",
  back: "browserActionBack",
  forward: "browserActionForward",
  reload: "browserActionReload",
  tabs: "browserActionTabs",
  new_tab: "browserActionNewTab",
  switch_tab: "browserActionSwitchTab",
  close_tab: "browserActionCloseTab",
};

export function BrowserActionBadge({ action }: { action?: string }) {
  const t = useTranslations("ToolViews");
  if (!action) return null;
  const key = BROWSER_ACTION_LABEL_KEYS[action];
  return <Pill colorPalette="orange">{key ? t(key as Parameters<typeof t>[0]) : action}</Pill>;
}

function BrowserCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const action = asString(args.action);
  const text = asString(args.text);
  return (
    <FieldList>
      <InlineField label={t("computerAction")}>
        <BrowserActionBadge action={action} />
      </InlineField>
      {asString(args.url) && <InlineField label={t("url")}>{asString(args.url)}</InlineField>}
      {args.element != null && (
        <InlineField label={t("computerElement")}>
          <Mono>{asString(args.element)}</Mono>
        </InlineField>
      )}
      {asString(args.tab) && (
        <InlineField label={t("browserTab")}>
          <Mono>{asString(args.tab)}</Mono>
        </InlineField>
      )}
      {action === "find" && asString(args.query) && (
        <InlineField label={t("browserQuery")}>{asString(args.query)}</InlineField>
      )}
      {action === "select" && asString(args.option) && (
        <InlineField label={t("browserOption")}>{asString(args.option)}</InlineField>
      )}
      {action === "upload" && Array.isArray(args.paths) && args.paths.length > 0 && (
        <InlineField label={t("browserPaths")}>{asArray(args.paths).map(asString).join(", ")}</InlineField>
      )}
      {action === "drag" && args.to_element != null && (
        <InlineField label={t("computerElement")}>
          <Mono>{`${asString(args.element)} → ${asString(args.to_element)}`}</Mono>
        </InlineField>
      )}
      {action === "press" && asString(args.key) && (
        <InlineField label={t("browserKey")}>
          <Mono>{asString(args.key)}</Mono>
        </InlineField>
      )}
      {action === "scroll" && asString(args.direction) && (
        <InlineField label={t("browserDirection")}>{asString(args.direction)}</InlineField>
      )}
      {text && (
        <Field label={t("computerText")}>
          <Text fontSize="xs" whiteSpace="pre-wrap">{text}</Text>
        </Field>
      )}
    </FieldList>
  );
}

// A task's lifecycle status → a translation key and a colour, so it reads as a
// proper badge instead of the raw lowercase value the model emits.
function taskStatusAppearance(status: string): { key: string; palette: string } | null {
  switch (status.toLowerCase().replace(/[\s-]+/g, "_")) {
    case "completed": return null;
    case "in_progress": return { key: "statusInProgress", palette: "blue" };
    case "blocked": return { key: "statusBlocked", palette: "yellow" };
    case "cancelled":
    case "canceled": return { key: "statusCancelled", palette: "gray" };
    case "deleted": return { key: "statusDeleted", palette: "red" };
    case "pending": case "": return { key: "statusPending", palette: "gray" };
    default: return { key: "statusUnknown", palette: "gray" };
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
  const t = useTranslations("ToolViews");
  const appearance = taskStatusAppearance(status);
  return (
    <Card>
      <Flex align="center" gap={2} mb={body ? 1.5 : 0}>
        <Text textStyle="sectionLabel" flexShrink={0}>{label}</Text>
        <Box flex={1} />
        {appearance && <Pill colorPalette={appearance.palette}>{t(appearance.key as Parameters<typeof t>[0])}</Pill>}
      </Flex>
      {body && <MarkdownContent content={body} fontSize="xs" />}
      {dependencies.length > 0 && (
        <Flex align="center" gap={1} mt={1.5} flexWrap="wrap">
          <Text fontSize="2xs" color="fg.subtle">{t("dependsOn")}</Text>
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

// The artifact renders outside the card, so the call view just names what is
// being opened (its URL/path) — never the page contents.
function OpenArtifactCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const url = asString(args.url);
  const title = asString(args.title);
  return (
    <FieldList>
      {title && <InlineField label={t("title")}>{title}</InlineField>}
      {url && <InlineField label={t("source")}><Mono>{url}</Mono></InlineField>}
    </FieldList>
  );
}

function ReadTaskCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const taskId = asString(args.task_id);
  return (
    <FieldList>
      <InlineField label={t("taskId")}>{taskId || "—"}</InlineField>
    </FieldList>
  );
}

// Fields whose values are human prose (not identifiers/data) — rendered with the
// markdown renderer in the normal font rather than monospace.
const PROSE_FIELD_KEYS = new Set([
  "justification",
  "goal",
  "prompt",
  "reason",
  "summary",
  "message",
  "content",
  "instructions",
  "query",
]);

// Translation keys for raw argument/result key labels. Falls back to the raw key
// if unmapped. The actual label text is resolved through the ToolViews namespace.
const FIELD_LABEL_KEYS: Record<string, string> = {
  server: "fieldServer",
  tool_name: "fieldToolName",
  arguments: "fieldArguments",
  read_only: "readOnly",
  justification: "justification",
  risk: "risk",
  uri: "fieldUri",
  query: "query",
  result_count: "results",
  agent: "agent",
  prompt: "prompt",
  task_id: "taskId",
  task_identifier: "taskId",
  code: "fieldStatus",
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
  artifact: "fieldArtifact",
  answers: "answers",
};

function ReadFileCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={t("filePath")}>
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      {args.offset != null && <InlineField label={t("offset")}>{asString(args.offset)}</InlineField>}
      {args.limit != null && <InlineField label={t("limit")}>{asString(args.limit)}</InlineField>}
    </FieldList>
  );
}

// One shared inline-diff surface: a single scroll container (one maxH token + the
// `diff-scroll` hook) wrapping the diff viewer with one shared style object, so
// the edit-call preview and the file-edit result render identically. The font size
// resolves to the `xs` token via its CSS var rather than a raw pixel literal.
const DIFF_VIEWER_STYLES = {
  contentText: { fontSize: "var(--chakra-font-sizes-xs)", fontFamily: "var(--app-font-mono)" },
};

function DiffView({ oldValue, newValue }: { oldValue: string; newValue: string }) {
  const { colorMode } = useColorMode();
  return (
    <Box
      maxH={80}
      overflowY="auto"
      border="1px solid"
      borderColor="border"
      borderRadius="md"
      className="diff-scroll"
    >
      <ReactDiffViewer
        oldValue={oldValue}
        newValue={newValue}
        splitView={false}
        useDarkTheme={colorMode === "dark"}
        hideLineNumbers={false}
        showDiffOnly={false}
        compareMethod={DiffMethod.LINES}
        styles={DIFF_VIEWER_STYLES}
      />
    </Box>
  );
}

function EditFileCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const skipValidation = args.skip_validation === true;
  const replaceAll = args.replace_all === true;
  const find = asString(args.find);
  const replaceWith = asString(args.replace_with);
  const hasInlineDiff = find && replaceWith && find !== replaceWith;
  return (
    <FieldList>
      <InlineField label={t("filePath")}>
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      {skipValidation && <InlineField label={t("skipValidation")}>{t("yes")}</InlineField>}
      {replaceAll && <InlineField label={t("replaceAll")}>{t("yes")}</InlineField>}
      {find && (
        <Field label={t("find")}>
          <MonoBlock>{find}</MonoBlock>
        </Field>
      )}
      {replaceWith && (
        <Field label={t("replaceWith")}>
          <MonoBlock>{replaceWith}</MonoBlock>
        </Field>
      )}
      {hasInlineDiff && (
        <Field label={t("diff")}>
          <DiffView oldValue={find} newValue={replaceWith} />
        </Field>
      )}
    </FieldList>
  );
}

function WriteFileCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={t("filePath")}>
        <Mono>{asString(args.file_path)}</Mono>
      </InlineField>
      <Field label={t("content")}>
        <MonoBlock>{asString(args.content)}</MonoBlock>
      </Field>
    </FieldList>
  );
}

function SearchContentCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={t("pattern")}>
        <Mono>{asString(args.pattern)}</Mono>
      </InlineField>
      {args.include ? (
        <InlineField label={t("include")}>
          <Mono>{asString(args.include)}</Mono>
        </InlineField>
      ) : null}
      {args.path ? (
        <InlineField label={t("path")}>
          <Mono>{asString(args.path)}</Mono>
        </InlineField>
      ) : null}
    </FieldList>
  );
}

function FindFilesCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={t("pattern")}>
        <Mono>{asString(args.pattern)}</Mono>
      </InlineField>
    </FieldList>
  );
}

function FetchUrlCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={t("url")}>
        <Mono>{asString(args.url)}</Mono>
      </InlineField>
      {args.format ? <InlineField label={t("format")}>{asString(args.format)}</InlineField> : null}
      {args.timeout != null && <InlineField label={t("timeout")}>{t("secondsValue", { value: asString(args.timeout) })}</InlineField>}
    </FieldList>
  );
}

function LoadSkillCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  return (
    <FieldList>
      <InlineField label={t("skill")}>
        <Mono>{asString(args.name)}</Mono>
      </InlineField>
    </FieldList>
  );
}

function AskUserCallView({ args }: { args: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const questions = asArray(args.questions).map(asRecord);
  if (questions.length === 0) return null;
  return (
    <FieldList>
      {questions.map((item, index) => {
        const options = asArray(item.options).map(asRecord);
        const label = asString(item.header) || t("questionN", { number: index + 1 });
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
                {t("multiSelect")}
              </Text>
            ) : null}
          </Field>
        );
      })}
    </FieldList>
  );
}

function ReadFileResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  // The call already shows the file path, so the result only surfaces the line
  // range and the content (no duplicated Path field).
  const content = asString(data.content);
  const range = [asString(data.start_line), asString(data.end_line)].filter(Boolean).join("–");
  const total = asString(data.total_lines);
  return (
    <FieldList>
      {range && (
        <InlineField label={t("lines")}>
          {total ? t("linesOfTotal", { range, total }) : range}
        </InlineField>
      )}
      {content && (
        <Field label={t("content")}>
          <MonoBlock>{content}</MonoBlock>
        </Field>
      )}
    </FieldList>
  );
}

function MatchListResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  // Pattern is already shown on the call card — only surface the count + matches.
  const matches = asArray(data.matches).map(asString);
  const count = asString(data.count) || String(matches.length);
  return (
    <FieldList>
      <InlineField label={t("count")}>{count}</InlineField>
      {matches.length > 0 && (
        <Field label={t("matches")}>
          <MonoBlock>{matches.join("\n")}</MonoBlock>
        </Field>
      )}
    </FieldList>
  );
}

function FileEditResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  // Shared by edit_file and write_file. Path is on the call card.
  const code = asString(data.code);
  const characters = asString(data.characters);
  const operationsApplied = asString(data.operations_applied);
  const created = data.created;
  const diagnostic = asRecord(data.diagnostic);
  const message = asString(data.message);
  const before = asString(data.before);
  const after = asString(data.after);

  // Old-format write_file response still carries before/after for inline diff.
  const hasDiff = before !== after && (before !== "" || after !== "");

  if (code === "edit_find_not_found" || code === "edit_find_near_miss" || code === "edit_find_not_unique") {
    const occurrences = asString(data.occurrences);
    return (
      <FieldList>
        <InlineField label={t("match")}>
          <Pill colorPalette="red">{code === "edit_find_not_unique" ? t("notUnique") : t("notFound")}</Pill>
        </InlineField>
        {code === "edit_find_not_unique" && occurrences && (
          <InlineField label={t("occurrences")}>{occurrences}</InlineField>
        )}
        {message && (
          <Field label={t("reason")}>
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
        <InlineField label={t("validation")}>
          <Pill colorPalette="red">{t("failed")}</Pill>
        </InlineField>
        <InlineField label={t("origin")}>{asString(diagnostic.origin)}</InlineField>
        <InlineField label={t("language")}>{asString(diagnostic.language)}</InlineField>
        {asString(diagnostic.line) && (
          <InlineField label={t("line")}>{asString(diagnostic.line)}:{asString(diagnostic.column)}</InlineField>
        )}
        <InlineField label={t("error")}>{asString(diagnostic.message)}</InlineField>
        {contextLines.length > 0 && (
          <Field label={t("context")}>
            <MonoBlock maxH={32}>{contextLines.join("\n")}</MonoBlock>
          </Field>
        )}
        {message && (
          <Field label={t("recovery")}>
            <Text fontSize="xs" color="fg.subtle">{message}</Text>
          </Field>
        )}
      </FieldList>
    );
  }

  return (
    <FieldList>
      {created != null && (
        <InlineField label={t("created")}>{created ? t("yes") : t("no")}</InlineField>
      )}
      {operationsApplied && code === "edit_completed" && (
        <InlineField label={t("operations")}>{operationsApplied}</InlineField>
      )}
      {characters && <InlineField label={t("characters")}>{characters}</InlineField>}
      {hasDiff ? <DiffView oldValue={before} newValue={after} /> : null}
    </FieldList>
  );
}

function FetchUrlResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  // URL + format are on the call card; only surface truncation + fetched content.
  const content = asString(data.content);
  return (
    <FieldList>
      {data.truncated === true && <InlineField label={t("truncated")}>{t("yes")}</InlineField>}
      {content && (
        <Field label={t("content")}>
          <MarkdownContent content={content} fontSize="xs" />
        </Field>
      )}
    </FieldList>
  );
}

function LoadSkillResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  // Skill name is on the call card; the internal resolved `path` is dropped.
  const title = asString(data.title);
  const content = asString(data.content);
  return (
    <FieldList>
      {title && <InlineField label={t("title")}>{title}</InlineField>}
      {content && (
        <Field label={t("content")}>
          <MarkdownContent content={content} fontSize="xs" />
        </Field>
      )}
    </FieldList>
  );
}

function AskUserResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  // Answers arrive as a per-question array of labels (string | string[]). Flatten
  // into readable pills instead of dumping the raw JSON array.
  const answers = asArray(data.answers);
  const labels: string[] = [];
  for (const answer of answers) {
    if (Array.isArray(answer)) labels.push(...answer.map(asString));
    else labels.push(asString(answer));
  }
  const shown = labels.filter(Boolean);
  if (shown.length === 0) return <EmptyHint>{t("noAnswer")}</EmptyHint>;
  return (
    <FieldList>
      <Field label={t("answers")}>
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
  const t = useTranslations("ToolViews");
  const entries = Object.entries(data);
  if (entries.length === 0) return <EmptyHint>{t("noData")}</EmptyHint>;
  return (
    <FieldList>
      {entries.map(([key, value]) => (
        <InlineField key={key} label={FIELD_LABEL_KEYS[key] ? t(FIELD_LABEL_KEYS[key] as Parameters<typeof t>[0]) : key}>
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

export function ToolCallView({ name, args, agents = [] }: { name: string; args?: Record<string, unknown>; agents?: { id: string; name: string; title?: string }[] }) {
  const t = useTranslations("ToolViews");
  if (!args) return null;
  const justification = asString(args.justification);
  const specificView = (() => {
    switch (name) {
      case "bash":
        return <BashCallView args={args} />;
      case "web_search":
        return <WebSearchCallView args={args} />;
      case "spawn_agent":
        return <SpawnAgentCallView args={args} agents={agents} />;
      case "set_tasks":
        return <WriteTasksCallView args={args} />;
      case "update_tasks":
        return <UpdateTasksCallView args={args} />;
      case "read_task":
        return <ReadTaskCallView args={args} />;
      case "open_artifact":
        return <OpenArtifactCallView args={args} />;
      case "read_file":
        return <ReadFileCallView args={args} />;
      case "edit_file":
        return <EditFileCallView args={args} />;
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
      case "computer":
        return <ComputerCallView args={args} />;
      case "browser":
        return <BrowserCallView args={args} />;
      default: {
        // The wrapper below already renders `justification` for every tool. Strip it here,
        // or tools without a dedicated view (MCP calls) would show it twice.
        const { justification: _justification, ...rest } = args;
        return <GenericView data={rest} />;
      }
    }
  })();
  if (!justification) return specificView;
  return (
    <FieldList>
      <Field label={t("justification")}>
        <MarkdownContent content={justification} fontSize="xs" />
      </Field>
      <Box>{specificView}</Box>
    </FieldList>
  );
}

// Tool result (output) views

function BashResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const output = asString(data.output);
  const outputFile = asString(data.output_file);
  const hasMeta = data.pid != null || data.size != null;
  if (!output && !outputFile && !hasMeta) return null;
  return (
    <FieldList>
      {data.pid != null && <InlineField label={t("pid")}>{asString(data.pid)}</InlineField>}
      {data.size != null && <InlineField label={t("size")}>{t("bytesValue", { value: asString(data.size) })}</InlineField>}
      {data.truncated === true && <InlineField label={t("truncated")}>{t("yes")}</InlineField>}
      {output ? (
        <Field label={t("output")}>
          <MonoBlock>{output}</MonoBlock>
        </Field>
      ) : outputFile ? (
        <InlineField label={t("output")}>{t("writtenTo", { file: outputFile })}</InlineField>
      ) : null}
      {output && outputFile ? <InlineField label={t("fullOutput")}><Mono>{outputFile}</Mono></InlineField> : null}
      {asString(data.full_output_file) ? <InlineField label={t("fullOutput")}><Mono>{asString(data.full_output_file)}</Mono></InlineField> : null}
    </FieldList>
  );
}

function WebResultCard({ result }: { result: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const title = asString(result.title) || t("untitled");
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
function WebSearchResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const results = asArray(data.results).map(asRecord);
  if (results.length === 0) return <EmptyHint>{t("noResults")}</EmptyHint>;
  return (
    <Flex direction="column" gap={1.5}>
      {results.map((result, index) => <WebResultCard key={index} result={result} />)}
    </Flex>
  );
}

// A spawned agent returns its A2A task as the tool result; show its
// deliverable (artifact).
function AgentTaskResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const task = data as unknown as A2ATask;
  return (
    <FieldList>
      <Field label={t("response")}>
        <MarkdownContent content={taskArtifactText(task)} />
      </Field>
    </FieldList>
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

function ErrorView({ message }: { message: string }) {
  return (
    <AlertBox colorPalette="red">
      <Text fontSize="xs" color="red.fg">{message}</Text>
    </AlertBox>
  );
}

// Shown when the browser tool can't reach Chrome because remote debugging is off: a brief message,
// the exact address, and a one-click button that opens that settings page in the user's browser.
function BrowserRemoteDebuggingAlert({ address, browserName }: { address: string; browserName?: string }) {
  const t = useTranslations("ToolViews");
  const [opened, setOpened] = useState(false);
  return (
    <AlertBox colorPalette="yellow">
      <Text textStyle="fieldLabel">{t("browserEnableTitle")}</Text>
      <Text fontSize="xs" color="fg.muted" mt={0.5}>{t("browserEnableBody")}</Text>
      <Flex align="center" gap={2} mt={2}>
        <Button
          size="xs"
          colorPalette="yellow"
          variant="solid"
          onClick={async () => setOpened(await openBrowserRemoteDebugging(browserName || "chrome"))}
        >
          <LuExternalLink size={12} />
          {t("browserEnableButton")}
        </Button>
        <Mono fontSize="2xs" color="fg.subtle">{address}</Mono>
      </Flex>
      {opened && <Text fontSize="2xs" color="green.fg" mt={1.5}>{t("browserEnableOpened")}</Text>}
    </AlertBox>
  );
}

// Shown when a tool needs a macOS privacy grant (Accessibility, Screen Recording): the same
// in-chat alert language as the remote-debugging one, with a brief message and a one-click
// button that surfaces the system prompt and opens the right System Settings pane.
function PermissionGrantAlert({ kind }: { kind: string }) {
  const t = useTranslations("ToolViews");
  const [opened, setOpened] = useState(false);
  const isScreenRecording = kind === "screen_recording";
  return (
    <AlertBox colorPalette="yellow">
      <Text textStyle="fieldLabel">
        {isScreenRecording ? t("permissionScreenRecordingTitle") : t("permissionAccessibilityTitle")}
      </Text>
      <Text fontSize="xs" color="fg.muted" mt={0.5}>
        {isScreenRecording ? t("permissionScreenRecordingBody") : t("permissionAccessibilityBody")}
      </Text>
      <Flex align="center" gap={2} mt={2}>
        <Button
          size="xs"
          colorPalette="yellow"
          variant="solid"
          onClick={async () => {
            await (isScreenRecording ? openScreenRecordingSettings() : openAccessibilitySettings());
            setOpened(true);
          }}
        >
          <LuExternalLink size={12} />
          {t("permissionGrantButton")}
        </Button>
      </Flex>
      {opened && <Text fontSize="2xs" color="green.fg" mt={1.5}>{t("permissionOpened")}</Text>}
    </AlertBox>
  );
}

// read_task returns either an error code (no id match / unavailable) or the
// task object itself (kind === "task"). The id is already shown on the call
// card, so the result only surfaces the outcome — it never re-renders the id.
function ReadTaskResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const code = asString(data.code);
  if (code === "task_not_found") return <ErrorView message={t("taskNotFound")} />;
  if (code === "read_task_unavailable") {
    return <EmptyHint>{t("readTaskUnavailable")}</EmptyHint>;
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
  // Inline (model-authored) HTML stays tightly sandboxed — no same-origin. A page
  // loaded by reference (a local file or a proxied external site) is a real
  // mini-browser, so it gets the fuller browsing set (forms, modals, downloads,
  // popups) it needs to behave like a native tab.
  return usesInlineHtml
    ? "allow-scripts allow-popups"
    : "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-forms allow-modals allow-downloads allow-presentation";
}

function normalizeArtifact(value: unknown): Record<string, unknown> | null {
  const artifact = asRecord(value);
  if (Object.keys(artifact).length === 0) return null;
  let type = asString(artifact.type).toLowerCase();
  // `open_artifact` results wrap the render kind in `type:"artifact"` with a
  // separate `kind` (image/html/iframe/file); fold that kind in as the render type.
  if (type === "artifact") type = asString(artifact.kind).toLowerCase();
  if (!type) {
    if (asString(artifact.src) || asString(artifact.srcdoc) || asString(artifact.file)) type = "iframe";
    else if (asString(artifact.html)) type = "html";
    else if (asString(artifact.data) || asString(artifact.url)) type = "image";
  }
  if (!["iframe", "html", "image", "link", "file"].includes(type)) return null;
  return { ...artifact, type };
}

function collectArtifacts(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.flatMap(collectArtifacts);
  const direct = normalizeArtifact(value);
  return direct ? [direct] : [];
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

// An artifact's title/label may contain markdown, so it is rendered through the
// markdown renderer above the artifact body.
function ArtifactFrame({ title, showHeader = true, fillContainer = false, children }: { title: string; showHeader?: boolean; fillContainer?: boolean; children: ReactNode }) {
  const t = useTranslations("ToolViews");
  // Remounting the content (via a changing key) forces a fresh fetch of the
  // rendered page — the /artifact-page route is served no-store, so the iframe
  // reloads the current file/URL rather than showing a stale render. Works for every
  // artifact kind (iframe, inline html, image) since each is a child subtree.
  const [reloadKey, setReloadKey] = useState(0);
  return (
    <Box h={fillContainer ? "100%" : undefined} display={fillContainer ? "flex" : undefined} flexDirection={fillContainer ? "column" : undefined}>
      {showHeader && (
        <Flex align="center" gap={1.5} mb={1.5} flexShrink={0}>
          <Text textStyle="panelTitle" color="fg.muted" flex={1} minW={0} truncate>
            {title}
          </Text>
          <IconButton
            aria-label={t("reloadArtifact")}
            title={t("reloadArtifact")}
            variant="ghost"
            boxSize="8"
            flexShrink={0}
            onClick={() => setReloadKey((current) => current + 1)}
          >
            <LuRotateCw size={14} />
          </IconButton>
        </Flex>
      )}
      <Box key={reloadKey} {...(fillContainer ? { flex: 1, minH: 0 } : {})}>{children}</Box>
    </Box>
  );
}

// A generous floor: model-authored artifacts frequently mis-measure their own
// height (absolute-positioned maps, late-loading content) and collapse to a few
// pixels. Never render one shorter than this, whether auto-sized or hand-resized.
const AUTO_MINIMUM_HEIGHT = 480;
const AUTO_MAXIMUM_HEIGHT = 960;

// A sandboxed iframe (in a bordered frame) that listens for back-channel messages
// the artifact posts to its parent. Messages are accepted only from this frame's own
// contentWindow and only when tagged `source: "artifact"`. Two kinds:
//   - `__artifact_resize` → sizes the frame to the content (when autoHeight), so the
//     model never has to guess a height. Never forwarded to the agent.
//   - anything else → a real interaction, forwarded (with the artifact's id/title)
//     to the active ArtifactEvent handler, becoming a follow-up turn for the agent.
function ArtifactSandbox({
  src,
  srcDoc,
  sandbox,
  title,
  artifactId,
  autoHeight,
  fixedHeight,
  fillContainer = false,
}: {
  src?: string;
  srcDoc?: string;
  sandbox: string;
  title: string;
  artifactId: string;
  autoHeight: boolean;
  fixedHeight: string;
  fillContainer?: boolean;
}) {
  const t = useTranslations("ToolViews");
  const frameRef = useRef<HTMLIFrameElement>(null);
  const onArtifactEvent = useArtifactEvent();
  const [measuredHeight, setMeasuredHeight] = useState<number | null>(null);

  useEffect(() => {
    function handleMessage(messageEvent: MessageEvent) {
      const frame = frameRef.current;
      if (!frame || messageEvent.source !== frame.contentWindow) return;
      const payload = messageEvent.data;
      if (!payload || typeof payload !== "object") return;
      const record = payload as Record<string, unknown>;
      if (record.source !== "artifact") return;
      if (record.event === "__artifact_resize") {
        if (autoHeight) {
          const reported = Number((record.data as Record<string, unknown> | undefined)?.height);
          if (Number.isFinite(reported)) {
            setMeasuredHeight(Math.max(AUTO_MINIMUM_HEIGHT, Math.min(AUTO_MAXIMUM_HEIGHT, Math.ceil(reported))));
          }
        }
        return; // internal sizing signal — never an agent-facing event
      }
      if (!onArtifactEvent) return;
      const eventName = typeof record.event === "string" ? record.event.trim() : "";
      if (!eventName) return;
      onArtifactEvent({ artifactId, title, event: eventName, data: record.data });
    }
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [onArtifactEvent, artifactId, title, autoHeight]);

  // A manual drag overrides auto-sizing entirely — the model's own resize logic
  // often collapses or mis-sizes an artifact, so the user can always take over.
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

  if (fillContainer) {
    return (
      <Box position="relative" w="100%" h="100%">
        <Box w="100%" h="100%" overflow="hidden">
          <iframe
            ref={frameRef}
            src={src || undefined}
            srcDoc={srcDoc || undefined}
            sandbox={sandbox}
            referrerPolicy="no-referrer"
            loading="lazy"
            title={title}
            style={{ width: "100%", height: "100%", border: 0, display: "block" }}
          />
        </Box>
      </Box>
    );
  }

  return (
    <Box position="relative" w="100%">
      <Box
        w="100%"
        h={`${effectiveHeight}px`}
        border="1px solid"
        borderColor="border"
        borderRadius="md"
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
        bottom={-1}
        h={2.5}
        cursor="ns-resize"
        display="flex"
        alignItems="center"
        justifyContent="center"
        role="separator"
        aria-label={t("resizeArtifactHeight")}
        _hover={{ "& > div": { bg: "fg.muted" } }}
      />
    </Box>
  );
}

export interface ArtifactImageAnnotationControls {
  image: ArtifactImageIdentity;
  annotations: ArtifactImageAnnotation[];
  onChange: (annotations: ArtifactImageAnnotation[]) => void;
  // When set, existing annotations are shown but cannot be added, edited, or removed
  // (e.g. displaying the annotations already sent with an image in the transcript).
  readOnly?: boolean;
}

interface DraftAnnotation {
  id: string;
  sequence: number;
  x: number;
  y: number;
  xRatio: number;
  yRatio: number;
  imageWidth: number;
  imageHeight: number;
  comment: string;
  createdAt: string;
  updatedAt: string;
}

function nextAnnotationSequence(annotations: ArtifactImageAnnotation[], id: string): number {
  const existing = annotations.find((annotation) => annotation.id === id);
  if (existing) return existing.sequence;
  return annotations.reduce((highest, annotation) => Math.max(highest, annotation.sequence), 0) + 1;
}

function ImageAnnotationPanel({
  draft,
  onCommentChange,
  onDelete,
  onDone,
}: {
  draft: DraftAnnotation;
  onCommentChange: (comment: string) => void;
  onDelete: () => void;
  onDone: () => void;
}) {
  const t = useTranslations("ToolViews");
  const horizontalOffset = draft.xRatio > 0.68 ? "calc(-100% - 14px)" : "14px";
  const verticalOffset = draft.yRatio > 0.68 ? "calc(-100% + 14px)" : "14px";
  return (
    <Box
      position="absolute"
      zIndex={6}
      left={`${draft.xRatio * 100}%`}
      top={`${draft.yRatio * 100}%`}
      transform={`translate(${horizontalOffset}, ${verticalOffset})`}
      w="min(340px, calc(100vw - 48px))"
      bg="bg"
      border="1px solid"
      borderColor="border.emphasized"
      borderRadius="md"
      boxShadow="lg"
      p={2.5}
      onPointerDown={(event: ReactPointerEvent) => event.stopPropagation()}
      onClick={(event: ReactMouseEvent) => event.stopPropagation()}
    >
      <Text fontSize="xs" fontWeight="semibold" mb={2}>
        {t("annotation")}
      </Text>
      <Textarea
        value={draft.comment}
        onChange={(event) => onCommentChange(event.target.value)}
        onKeyDown={(event) => {
          // Enter saves and closes the annotation; Shift+Enter inserts a newline.
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            onDone();
          }
        }}
        placeholder={t("annotationPlaceholder")}
        fontSize="sm"
        bg="bg.subtle"
        minH={16}
        autoFocus
      />
      <Flex align="center" justify="flex-end" gap={2} mt={2}>
        <Button aria-label={t("deleteAnnotation")} title={t("deleteAnnotation")} variant="ghost" colorPalette="red" onClick={onDelete}>
          <LuTrash2 size={12} />
          {t("removeIt")}
        </Button>
        <Button aria-label={t("saveAnnotation")} title={t("saveAnnotation")} variant="solid" colorPalette="blue" onClick={onDone}>
          <LuCheck size={12} />
          {t("saveIt")}
        </Button>
      </Flex>
    </Box>
  );
}

function AnnotatableImage({
  source,
  title,
  artifactHeightValue,
  fillContainer,
  annotationsControl,
}: {
  source: string;
  title: string;
  artifactHeightValue: string;
  fillContainer: boolean;
  annotationsControl?: ArtifactImageAnnotationControls;
}) {
  const t = useTranslations("ToolViews");
  const imageRef = useRef<HTMLImageElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // The panned content box — gets a dampened translate when a drag pushes past the scroll
  // bounds (rubber-band overscroll), then animates back to the fitted position on release.
  const contentRef = useRef<HTMLDivElement>(null);
  // The image point (as a ratio) under the cursor at the moment of a zoom, plus the
  // cursor's viewport position — so a layout effect can re-anchor it after the resize.
  const zoomFocusRef = useRef<{ xRatio: number; yRatio: number; clientX: number; clientY: number } | null>(null);
  // An in-progress click-drag pan of the (zoomed) image: where the drag began, the
  // scroll offset at that moment, and whether it has moved past the click threshold —
  // once it has, the pointer-up that follows is a pan, not an annotation click.
  const panRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    scrollLeft: number;
    scrollTop: number;
    moved: boolean;
  } | null>(null);
  // Set for exactly one click: the click that a completed drag emits on release, so we can
  // swallow it without dropping an annotation. Cleared by that click (or the next press).
  const suppressClickRef = useRef(false);
  const [panning, setPanning] = useState(false);
  const [naturalSize, setNaturalSize] = useState<{ width: number; height: number } | null>(null);
  const [fitZoom, setFitZoom] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [draft, setDraft] = useState<DraftAnnotation | null>(null);
  const annotations = annotationsControl?.annotations ?? [];
  const readOnly = Boolean(annotationsControl?.readOnly);
  const editable = Boolean(annotationsControl) && !readOnly;
  const annotated = Boolean(annotationsControl && naturalSize);
  const effectiveZoom = fitZoom * zoom;
  const displayWidth = naturalSize ? Math.max(1, naturalSize.width * effectiveZoom) : undefined;
  const displayHeight = naturalSize ? Math.max(1, naturalSize.height * effectiveZoom) : undefined;

  function fittedZoomForImage(imageWidth: number, imageHeight: number): number {
    const container = scrollRef.current;
    if (!container || imageWidth <= 0 || imageHeight <= 0) return 1;
    const containerBounds = container.getBoundingClientRect();
    const availableWidth = Math.max(1, container.clientWidth || containerBounds.width);
    const availableHeight = Math.max(1, container.clientHeight || containerBounds.height);
    return Math.max(0.001, Number(Math.min(availableWidth / imageWidth, availableHeight / imageHeight).toFixed(4)));
  }

  function fitImageToViewport(imageSize = naturalSize) {
    if (imageSize) setFitZoom(annotationsControl ? fittedZoomForImage(imageSize.width, imageSize.height) : 1);
    setZoom(1);
  }

  function draftFromPoint(clientX: number, clientY: number, existing?: ArtifactImageAnnotation): DraftAnnotation | null {
    const image = imageRef.current;
    if (!image) return null;
    const bounds = image.getBoundingClientRect();
    const imageWidth = naturalSize?.width || image.naturalWidth;
    const imageHeight = naturalSize?.height || image.naturalHeight;
    if (!imageWidth || !imageHeight || bounds.width <= 0 || bounds.height <= 0) return null;
    const xRatio = clamp((clientX - bounds.left) / bounds.width, 0, 1);
    const yRatio = clamp((clientY - bounds.top) / bounds.height, 0, 1);
    const now = new Date().toISOString();
    const id = existing?.id ?? createClientIdentifier("annotation");
    return {
      id,
      // Base a new annotation's number on the list WITHOUT any still-empty draft, so
      // placing after abandoning a blank annotation reuses its number instead of skipping it.
      sequence: existing?.sequence ?? nextAnnotationSequence(annotationsDroppingEmptyDraft(), id),
      x: Math.round(xRatio * imageWidth),
      y: Math.round(yRatio * imageHeight),
      xRatio,
      yRatio,
      imageWidth,
      imageHeight,
      comment: existing?.comment ?? "",
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
    };
  }

  // Click-drag to pan the image around the scroll viewport (the usual behaviour for a
  // zoomed image). A drag only starts once the pointer moves past a small threshold, so a
  // plain click still falls through to placing an annotation.
  function handlePanStart(event: ReactPointerEvent<HTMLDivElement>) {
    const container = scrollRef.current;
    if (!container || event.button !== 0) return;
    suppressClickRef.current = false;
    // Cancel any in-flight snap-back so a fresh grab starts from rest, not mid-animation.
    if (contentRef.current) {
      contentRef.current.style.transition = "none";
      contentRef.current.style.transform = "";
    }
    panRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      scrollLeft: container.scrollLeft,
      scrollTop: container.scrollTop,
      moved: false,
    };
  }

  // How much of the past-the-edge drag the image visually follows (rubber-band resistance).
  const OVERSCROLL_RESISTANCE = 0.4;

  // Ease the rubber-banded content box back to its fitted position (translate → 0).
  function snapOverscrollBack() {
    const content = contentRef.current;
    if (!content) return;
    content.style.transition = "transform 260ms cubic-bezier(0.22, 1, 0.36, 1)";
    content.style.transform = "";
  }

  function handlePanMove(event: ReactPointerEvent<HTMLDivElement>) {
    const pan = panRef.current;
    const container = scrollRef.current;
    if (!pan || !container || event.pointerId !== pan.pointerId) return;
    if ((event.buttons & 1) !== 1) {
      panRef.current = null;
      setPanning(false);
      snapOverscrollBack();
      return;
    }
    const deltaX = event.clientX - pan.startX;
    const deltaY = event.clientY - pan.startY;
    if (!pan.moved && Math.hypot(deltaX, deltaY) < 4) return;
    if (!pan.moved) {
      pan.moved = true;
      suppressClickRef.current = true;
      setPanning(true);
      container.setPointerCapture?.(event.pointerId);
    }
    // Scroll clamps to the in-bounds range; the amount the drag asked for past that clamp
    // is rendered as a dampened translate so the image rubber-bands out of bounds.
    const desiredLeft = pan.scrollLeft - deltaX;
    const desiredTop = pan.scrollTop - deltaY;
    container.scrollLeft = desiredLeft;
    container.scrollTop = desiredTop;
    const overX = desiredLeft - container.scrollLeft;
    const overY = desiredTop - container.scrollTop;
    const content = contentRef.current;
    if (content) {
      content.style.transition = "none";
      content.style.transform =
        overX || overY ? `translate(${-overX * OVERSCROLL_RESISTANCE}px, ${-overY * OVERSCROLL_RESISTANCE}px)` : "";
    }
  }

  function handlePanEnd(event: ReactPointerEvent<HTMLDivElement>) {
    const pan = panRef.current;
    if (!pan || event.pointerId !== pan.pointerId) return;
    suppressClickRef.current = pan.moved;
    if (pan.moved && scrollRef.current?.hasPointerCapture?.(event.pointerId)) {
      scrollRef.current.releasePointerCapture(event.pointerId);
    }
    panRef.current = null;
    setPanning(false);
    snapOverscrollBack();
  }

  function handlePanCancel(event: ReactPointerEvent<HTMLDivElement>) {
    const pan = panRef.current;
    if (!pan || event.pointerId !== pan.pointerId) return;
    suppressClickRef.current = false;
    panRef.current = null;
    setPanning(false);
    snapOverscrollBack();
  }

  function consumeSuppressedAnnotationClick() {
    const pan = panRef.current;
    if (suppressClickRef.current || pan?.moved) {
      suppressClickRef.current = false;
      if (pan?.moved && scrollRef.current?.hasPointerCapture?.(pan.pointerId)) {
        scrollRef.current.releasePointerCapture(pan.pointerId);
      }
      panRef.current = null;
      setPanning(false);
      snapOverscrollBack();
      return true;
    }
    return false;
  }

  function updateDraftPosition(event: ReactMouseEvent<HTMLDivElement>) {
    if (consumeSuppressedAnnotationClick()) {
      return;
    }
    if (!annotationsControl || readOnly) return;
    const nextDraft = draftFromPoint(event.clientX, event.clientY);
    if (!nextDraft) return;
    const saved: ArtifactImageAnnotation = {
      id: nextDraft.id,
      sequence: nextDraft.sequence,
      x: nextDraft.x,
      y: nextDraft.y,
      xRatio: nextDraft.xRatio,
      yRatio: nextDraft.yRatio,
      imageWidth: nextDraft.imageWidth,
      imageHeight: nextDraft.imageHeight,
      comment: "",
      createdAt: nextDraft.createdAt,
      updatedAt: nextDraft.updatedAt,
    };
    annotationsControl.onChange([...annotationsDroppingEmptyDraft(), saved]);
    setDraft(saved);
  }

  function openExistingAnnotation(annotation: ArtifactImageAnnotation, event: ReactMouseEvent) {
    event.stopPropagation();
    if (consumeSuppressedAnnotationClick()) {
      return;
    }
    if (readOnly) return;
    // Leaving a still-empty draft to open a different annotation drops the empty one.
    if (annotationsControl && draft && draft.id !== annotation.id && !draft.comment.trim()) {
      annotationsControl.onChange(annotations.filter((existing) => existing.id !== draft.id));
    }
    setDraft({
      id: annotation.id,
      x: annotation.x,
      y: annotation.y,
      xRatio: annotation.xRatio,
      yRatio: annotation.yRatio,
      imageWidth: annotation.imageWidth,
      imageHeight: annotation.imageHeight,
      comment: annotation.comment,
      createdAt: annotation.createdAt,
      updatedAt: annotation.updatedAt,
      sequence: annotation.sequence,
    });
  }

  function updateDraftComment(comment: string) {
    if (!draft || !annotationsControl) return;
    const now = new Date().toISOString();
    const saved: ArtifactImageAnnotation = {
      id: draft.id,
      sequence: draft.sequence,
      x: draft.x,
      y: draft.y,
      xRatio: draft.xRatio,
      yRatio: draft.yRatio,
      imageWidth: draft.imageWidth,
      imageHeight: draft.imageHeight,
      comment,
      createdAt: draft.createdAt,
      updatedAt: now,
    };
    const nextAnnotations = annotations.some((annotation) => annotation.id === saved.id)
      ? annotations.map((annotation) => (annotation.id === saved.id ? saved : annotation))
      : [...annotations, saved];
    annotationsControl.onChange(nextAnnotations);
    setDraft(saved);
  }

  function deleteDraft() {
    if (!draft || !annotationsControl) return;
    annotationsControl.onChange(annotations.filter((annotation) => annotation.id !== draft.id));
    setDraft(null);
  }

  // An annotation with no comment is never kept. Returns the annotation list minus the
  // current draft when it is still empty — used whenever we leave a draft (close the popover,
  // place another, or open a different one) so a blank annotation is discarded rather than saved.
  function annotationsDroppingEmptyDraft(): ArtifactImageAnnotation[] {
    return draft && !draft.comment.trim()
      ? annotations.filter((annotation) => annotation.id !== draft.id)
      : annotations;
  }

  function handleWheel(event: ReactWheelEvent<HTMLDivElement>) {
    if (!annotated) return;
    event.preventDefault();
    const image = imageRef.current;
    const container = scrollRef.current;
    if (!image || !container) return;
    // Normalize the wheel delta across devices (line/page modes → pixels) so a trackpad
    // and a mouse wheel feel consistent, then zoom gently and multiplicatively.
    let deltaY = event.deltaY;
    if (event.deltaMode === 1) deltaY *= 16;
    else if (event.deltaMode === 2) deltaY *= image.clientHeight || 400;
    const imageBounds = image.getBoundingClientRect();
    const containerBounds = container.getBoundingClientRect();
    const focusClientX = deltaY > 0 ? containerBounds.left + containerBounds.width / 2 : event.clientX;
    const focusClientY = deltaY > 0 ? containerBounds.top + containerBounds.height / 2 : event.clientY;
    const xRatio = imageBounds.width > 0 ? clamp((focusClientX - imageBounds.left) / imageBounds.width, 0, 1) : 0.5;
    const yRatio = imageBounds.height > 0 ? clamp((focusClientY - imageBounds.top) / imageBounds.height, 0, 1) : 0.5;
    setZoom((current) => {
      const next = clamp(Number((current * Math.exp(-deltaY * 0.0015)).toFixed(3)), MINIMUM_RELATIVE_IMAGE_ZOOM, MAXIMUM_RELATIVE_IMAGE_ZOOM);
      if (next !== current) zoomFocusRef.current = { xRatio, yRatio, clientX: focusClientX, clientY: focusClientY };
      return next;
    });
  }

  // After zoom resizes the image, nudge the scroll so the focus point stays fixed.
  // Layout effect → applied before the browser paints.
  useLayoutEffect(() => {
    const focus = zoomFocusRef.current;
    const container = scrollRef.current;
    const image = imageRef.current;
    if (!focus || !container || !image) return;
    zoomFocusRef.current = null;
    const bounds = image.getBoundingClientRect();
    container.scrollLeft += bounds.left + focus.xRatio * bounds.width - focus.clientX;
    container.scrollTop += bounds.top + focus.yRatio * bounds.height - focus.clientY;
  }, [zoom]);

  // Re-fit the image whenever the viewport itself changes size (the user drags the
  // artifacts panel wider/narrower). Only the annotated path drives its own size off
  // `fitZoom`; a plain image already reflows via its CSS `max-width: 100%`. The base
  // fit is recomputed and the user's relative `zoom` multiplier is preserved, so a
  // fitted image re-fits to the new width and a zoomed one keeps its zoom.
  useEffect(() => {
    if (!annotationsControl) return;
    const container = scrollRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (naturalSize) setFitZoom(fittedZoomForImage(naturalSize.width, naturalSize.height));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [annotationsControl, naturalSize]);

  return (
    <Box
      position="relative"
      h={fillContainer ? "100%" : undefined}
      maxW="100%"
      maxH={fillContainer ? undefined : artifactHeightValue}
      border="0"
      borderRadius="0"
      bg="bg.muted"
      overflow="hidden"
    >
      {/* The scroll viewport. `safe center` centers the image when it fits and falls
          back to a scrollable top-left when it overflows — no reserved bands above or
          below the picture. */}
      <Box
        ref={scrollRef}
        h={fillContainer ? "100%" : undefined}
        maxH={fillContainer ? undefined : artifactHeightValue}
        overflow="auto"
        display="flex"
        alignItems="safe center"
        justifyContent="safe center"
        cursor={panning ? "grabbing" : editable ? "crosshair" : "grab"}
        userSelect="none"
        onWheel={handleWheel}
        onPointerDown={handlePanStart}
        onPointerMove={handlePanMove}
        onPointerUp={handlePanEnd}
        onPointerCancel={handlePanCancel}
      >
        <Box
          ref={contentRef}
          position="relative"
          flexShrink={0}
          w={displayWidth ? `${displayWidth}px` : "fit-content"}
          h={displayHeight ? `${displayHeight}px` : undefined}
          maxW={annotated ? undefined : "100%"}
          cursor={panning ? "grabbing" : editable ? "crosshair" : "grab"}
          onClick={updateDraftPosition}
        >
          {/* eslint-disable-next-line @next/next/no-img-element -- MCP image artifacts can be data URLs or arbitrary remote sources. */}
          <img
            ref={imageRef}
            src={source}
            alt={title}
            draggable={false}
            onDragStart={(event) => event.preventDefault()}
            onLoad={(event) => {
              const imageNaturalSize = {
                width: event.currentTarget.naturalWidth,
                height: event.currentTarget.naturalHeight,
              };
              fitImageToViewport(imageNaturalSize);
              setNaturalSize(imageNaturalSize);
            }}
            style={{
              width: displayWidth ? `${displayWidth}px` : "auto",
              height: displayHeight ? `${displayHeight}px` : "auto",
              maxWidth: annotated ? "none" : "100%",
              maxHeight: annotated ? "none" : artifactHeightValue,
              display: "block",
              userSelect: "none",
              WebkitUserSelect: "none",
            }}
          />
          {annotated && annotations.map((annotation) => (
          <Tooltip
            key={annotation.id}
            openDelay={150}
            positioning={{ placement: "top" }}
            disabled={!annotation.comment.trim() || draft?.id === annotation.id}
            rich
            contentProps={{ maxW: "340px" }}
            content={
              <Box>
                <Text fontWeight="semibold" mb={1} color="fg">{t("annotationNumber", { number: annotation.sequence })}</Text>
                <MarkdownContent content={annotation.comment} fontSize="xs" />
              </Box>
            }
          >
          <Box
            as="button"
            position="absolute"
            left={`${annotation.xRatio * 100}%`}
            top={`${annotation.yRatio * 100}%`}
            transform="translate(-50%, -50%)"
            boxSize={6}
            borderRadius="full"
            bg="blue.solid"
            color="white"
            // Match the comment-panel badge exactly: a native <button> uses the UA button font,
            // so force the app font here or the digit renders in a different typeface.
            fontFamily="var(--app-font-sans)"
            boxShadow={draft?.id === annotation.id ? "0 0 0 3px var(--chakra-colors-blue-solid)" : undefined}
            cursor={readOnly ? "default" : "pointer"}
            onClick={(event: ReactMouseEvent) => openExistingAnnotation(annotation, event)}
          >
            <CenteredNumber fontSize={12} weight={600}>{annotation.sequence}</CenteredNumber>
          </Box>
          </Tooltip>
        ))}
          {annotated && draft ? (
            <ImageAnnotationPanel
              draft={draft}
              onCommentChange={updateDraftComment}
              onDelete={deleteDraft}
              onDone={() => {
                // An annotation with nothing typed is discarded, not saved.
                if (draft && !draft.comment.trim()) deleteDraft();
                else setDraft(null);
              }}
            />
          ) : null}
        </Box>
      </Box>
    </Box>
  );
}

// When an artifact can't be rendered (no safe source yet, missing content) we show a quiet
// empty state rather than a red destructive error box — in the panel this also covers the
// brief moment while a version's bytes are still resolving, so there's no error flash. In
// the panel (fillContainer) it centers to fill; inline in the transcript it's a small hint.
function ArtifactUnavailable({ fillContainer, children }: { fillContainer?: boolean; children: ReactNode }) {
  const t = useTranslations("ToolViews");
  // In the panel (fillContainer) this is a real empty state — icon + title + hint, styled
  // exactly like the app's other panel empty states. Inline in the transcript it stays a
  // one-line hint.
  if (fillContainer) {
    return (
      <Flex flex={1} minH={0} align="center" justify="center">
        <PanelEmptyState icon={<LuImageOff />} title={children} description={t("nothingToPreviewHint")} />
      </Flex>
    );
  }
  return <EmptyHint>{children}</EmptyHint>;
}

function RenderArtifact({
  artifact,
  showHeader = true,
  fillContainer = false,
  annotationsControl,
}: {
  artifact: Record<string, unknown>;
  showHeader?: boolean;
  fillContainer?: boolean;
  annotationsControl?: ArtifactImageAnnotationControls;
}) {
  const t = useTranslations("ToolViews");
  const type = asString(artifact.type);
  const title = asString(artifact.title) || t("mcpArtifact");
  const artifactId = asString(artifact.artifact_id) || asString(artifact.artifactId) || asString(artifact.id);
  const isAutoHeight = artifact.height === "auto" || artifact.height == null || artifact.height === "";
  if (type === "iframe") {
    // A `file` reference (open_artifact of a local file) is served by the backend
    // /artifact-page route, which injects the runtime so the page can self-size — so
    // honor auto-height for it, the same as inline html. An external `src` is routed
    // through the /artifact-proxy route so sites that refuse direct framing still render.
    const file = asString(artifact.file);
    const externalSrc = safeWebUrl(asString(artifact.src));
    // A remote file's location + session ride along so the backend serves it (and its
    // relative assets) through that location's executor; a local file omits them.
    const baseSrc = file
      ? artifactPageUrl(file, { location: asString(artifact.location), session: asString(artifact.session) })
      : externalSrc ? artifactProxyUrl(externalSrc) : "";
    // An in-place refresh (replace) keeps the same artifact_id (and the same file
    // path/URL), so the iframe src would be byte-identical and the frame would never
    // reload — showing the previous render until a full page reload. The backend
    // stamps a fresh `version` on every artifact call; fold it into the src as a
    // cache-buster so a refresh actually reloads the (rewritten) page.
    const version = asString(artifact.version);
    const src = baseSrc && version
      ? `${baseSrc}${baseSrc.includes("?") ? "&" : "?"}v=${encodeURIComponent(version)}`
      : baseSrc;
    const srcDoc = asString(artifact.srcdoc);
    if (!src && !srcDoc) return <ArtifactUnavailable fillContainer={fillContainer}>{t("nothingToPreview")}</ArtifactUnavailable>;
    return (
      <ArtifactFrame title={title} showHeader={showHeader} fillContainer={fillContainer}>
        <ArtifactSandbox
          src={src || undefined}
          srcDoc={srcDoc || undefined}
          sandbox={artifactSandbox(artifact, Boolean(srcDoc))}
          title={title}
          artifactId={artifactId}
          autoHeight={Boolean(file) && isAutoHeight}
          // An external page can't report its height, so give it a generous default
          // frame (the user can still drag to resize) instead of the short fallback.
          fixedHeight={!file && isAutoHeight ? "100vh" : artifactHeight(artifact.height)}
          fillContainer={fillContainer}
        />
      </ArtifactFrame>
    );
  }
  if (type === "html") {
    const html = asString(artifact.html) || asString(artifact.srcdoc);
    if (!html) return <ArtifactUnavailable fillContainer={fillContainer}>{t("nothingToPreview")}</ArtifactUnavailable>;
    return (
      <ArtifactFrame title={title} showHeader={showHeader} fillContainer={fillContainer}>
        <ArtifactSandbox
          srcDoc={html}
          sandbox={artifactSandbox(artifact, true)}
          title={title}
          artifactId={artifactId}
          autoHeight={isAutoHeight}
          fixedHeight={artifactHeight(artifact.height)}
          fillContainer={fillContainer}
        />
      </ArtifactFrame>
    );
  }
  if (type === "image") {
    const file = asString(artifact.file);
    // Prefer an explicit image source (a versioned blob-bytes URL, an http image, or a
    // data: URI); otherwise serve the file itself through /artifact-page, which renders a
    // freshly-opened local image (before its first version blob is captured) and remote
    // images. This is the same serve path HTML uses, so images no longer fall through to
    // an empty "nothing to preview" when the blob URL is not yet available.
    const explicit = safeImageSource(asString(artifact.data) || asString(artifact.src) || asString(artifact.url));
    const source = explicit || (file ? artifactPageUrl(file, { location: asString(artifact.location), session: asString(artifact.session) }) : "");
    if (!source) return <ArtifactUnavailable fillContainer={fillContainer}>{t("nothingToPreview")}</ArtifactUnavailable>;
    const imageIdentity = imageIdentityForArtifact({ ...artifact, type });
    return (
      <ArtifactFrame title={title} showHeader={showHeader} fillContainer={fillContainer}>
        <AnnotatableImage
          source={source}
          title={title}
          artifactHeightValue={artifactHeight(artifact.height)}
          fillContainer={fillContainer}
          annotationsControl={imageIdentity?.key === annotationsControl?.image.key ? annotationsControl : undefined}
        />
      </ArtifactFrame>
    );
  }
  if (type === "file") {
    // A code/text file artifact. The rich diff/version view lives in the Artifacts
    // panel; inline in the transcript we just name it and point at its bytes.
    const source = safeWebUrl(asString(artifact.src) || asString(artifact.url));
    return (
      <ArtifactFrame title={title} showHeader={showHeader}>
        {source ? (
          <Link href={source} target="_blank" rel="noopener noreferrer" colorPalette="blue">
            {title}
          </Link>
        ) : (
          <Text fontSize="xs" color="fg.muted">{title}</Text>
        )}
      </ArtifactFrame>
    );
  }
  const href = safeWebUrl(asString(artifact.href) || asString(artifact.url) || asString(artifact.src));
  if (!href) return <ErrorView message={t("linkNoSafeUrl")} />;
  return (
    <ArtifactFrame title={title} showHeader={showHeader}>
      <Link href={href} target="_blank" rel="noopener noreferrer" colorPalette="blue">
        {href}
      </Link>
    </ArtifactFrame>
  );
}

export function ArtifactView({
  artifact,
  showHeader = true,
  fillContainer = false,
  annotationsControl,
}: {
  artifact: Record<string, unknown>;
  showHeader?: boolean;
  fillContainer?: boolean;
  annotationsControl?: ArtifactImageAnnotationControls;
}) {
  return <RenderArtifact artifact={artifact} showHeader={showHeader} fillContainer={fillContainer} annotationsControl={annotationsControl} />;
}

// The external http(s) URL an artifact points at, or "" when it is not an external
// web page (a local file, inline HTML, an image). The Artifacts panel uses this to
// decide whether to hand the render to the native webview (desktop) instead of the
// proxied iframe.
export function externalArtifactUrl(artifact: Record<string, unknown>): string {
  const normalized = normalizeArtifact(artifact);
  if (normalized?.type !== "iframe") return "";
  if (asString(artifact.file) || asString(artifact.srcdoc)) return "";
  return safeWebUrl(asString(artifact.src));
}

// Renderable artifacts (maps, images, HTML) returned by an MCP tool/resource.
// These are rendered OUTSIDE the tool-call card so they stay visible, rather than
// being tucked inside its collapsible body.
export function extractToolArtifacts(name: string, content: string): Record<string, unknown>[] {
  if (name !== "call_mcp_tool" && name !== "read_mcp_resource" && name !== "open_artifact") return [];
  const parsed = tryParse(content);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  return collectArtifacts((parsed as Record<string, unknown>).artifacts);
}

// Only iframe/html artifacts mount a live frame (scripts, network, layout). Images
// and links are cheap, so the "one live frame at a time" rule only applies here.
export function isLiveArtifact(artifact: Record<string, unknown>): boolean {
  const normalized = normalizeArtifact(artifact);
  return normalized?.type === "iframe" || normalized?.type === "html";
}

export function isArtifactPanelArtifact(artifact: Record<string, unknown>): boolean {
  const normalized = normalizeArtifact(artifact);
  return normalized?.type === "iframe" || normalized?.type === "html" || normalized?.type === "image";
}

// A collapsed, click-to-open stand-in for a live artifact that is not currently
// active. Unmounting the iframe stops its scripts and network activity, which is
// what keeps a long transcript (many artifacts) from dragging the page down.
function CollapsedArtifact({ title, onOpen }: { title: string; onOpen: () => void }) {
  const t = useTranslations("ToolViews");
  return (
    <Flex
      as="button"
      align="center"
      gap={1.5}
      px={2}
      py={1.5}
      borderRadius="md"
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
      <Text textStyle="fieldLabel" flex={1} minW={0} truncate>{title}</Text>
      <Text fontSize="2xs" color="fg.subtle" flexShrink={0}>{t("clickToOpen")}</Text>
    </Flex>
  );
}

export function ToolArtifacts({
  artifacts,
  openTabIds,
  onOpenTab,
  toolCallId,
}: {
  artifacts: Record<string, unknown>[];
  // The set of toolCallIds whose artifacts are already open as tabs in the Artifacts
  // panel. Artifacts not yet opened show as collapsed click-to-open placeholders.
  openTabIds?: Set<string>;
  onOpenTab?: (toolCallId: string) => void;
  toolCallId?: string;
}) {
  const t = useTranslations("ToolViews");
  if (artifacts.length === 0) return null;
  // If this tool call's artifact is already an open tab, mount it live; otherwise
  // show a collapsed placeholder the user can click to open.
  const callOpen = toolCallId ? openTabIds?.has(toolCallId) : false;
  return (
    <Flex direction="column" gap={1.5}>
      {artifacts.map((artifact, index) => {
        const key = asString(artifact.artifact_id) || asString(artifact.artifactId) || asString(artifact.id) || String(index);
        if (isLiveArtifact(artifact) && !callOpen) {
          const title = asString(artifact.title) || t("artifactFallback");
          return <CollapsedArtifact key={key} title={title} onOpen={() => onOpenTab?.(toolCallId ?? "")} />;
        }
        return <RenderArtifact key={key} artifact={artifact} />;
      })}
    </Flex>
  );
}

// The MCP result shown inside the tool-call card. Artifacts are rendered
// separately (outside the card), so this only surfaces the tool's textual output.
function McpResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  if (data.is_error === true) {
    return <ErrorView message={t("mcpToolError")} />;
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

// The computer tool's result is one of a few shapes: an error, an observe snapshot, or a
// one-line action confirmation. The app isn't repeated here — the call above already names it.
// Element lists (the observe payload and the diff) are shown as their raw JSON in a code block,
// like a diff or file body: no per-row assembly, just the data.
function ComputerResultView({ data }: { data: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  if (data.ok === false) {
    // A missing macOS privacy grant is not a failure to report but a thing to fix, so
    // render the grant flow instead of a red error box.
    const neededPermission = asString(data.needs_permission);
    if (neededPermission) return <PermissionGrantAlert kind={neededPermission} />;
    return <ErrorView message={asString(data.error) || t("failed")} />;
  }
  const elements = asArray(data.elements);
  const did = asString(data.did);

  // Observe snapshot.
  if (data.count != null || elements.length > 0) {
    const durationMs = Number(data.duration_ms);
    const hint = asString(data.hint);
    const changes = asRecord(data.changes_since_last_observe);
    const changeGroups: [string, unknown[]][] = [
      [t("computerAppeared"), asArray(changes.appeared)],
      [t("computerDisappeared"), asArray(changes.disappeared)],
      [t("computerChanged"), asArray(changes.changed)],
    ];
    return (
      <FieldList>
        {asString(data.window) && <InlineField label={t("computerWindow")}>{asString(data.window)}</InlineField>}
        {data.count != null && <InlineField label={t("computerTotalElements")}>{asString(data.count)}</InlineField>}
        {Number.isFinite(durationMs) && (
          <InlineField label={t("computerDuration")}>{t("computerMs", { value: Math.round(durationMs) })}</InlineField>
        )}
        {hint && <EmptyHint>{hint}</EmptyHint>}
        {changeGroups.map(([label, items]) =>
          items.length > 0 ? (
            <Field key={label} label={label}>
              <MonoBlock>{JSON.stringify(items, null, 2)}</MonoBlock>
            </Field>
          ) : null
        )}
        {elements.length > 0 && (
          <Field label={t("computerElements")}>
            <MonoBlock>{JSON.stringify(elements, null, 2)}</MonoBlock>
          </Field>
        )}
      </FieldList>
    );
  }

  // Action confirmation (click / type / key / menu / scroll / launch).
  if (did) {
    return (
      <FieldList>
        <InlineField label={t("computerResult")}>{did}</InlineField>
      </FieldList>
    );
  }
  return null;
}

// The browser tool's result: an error, a tab listing, a page read, a page overview
// (title / url / element list as JSON, like the computer tool), or an action confirmation.
// The URL row is suppressed when it merely echoes the call's own `url` argument (the call
// card already shows it); it appears only when the browser ended up somewhere else.
function BrowserResultView({ data, args }: { data: Record<string, unknown>; args?: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const normalizeUrl = (value: string) => value.replace(/\/+$/, "");
  const requestedUrl = normalizeUrl(asString(args?.url));
  const resultUrl = asString(data.url);
  const showUrl = Boolean(resultUrl) && (normalizeUrl(resultUrl) !== requestedUrl || data.url_changed === true);
  if (data.ok === false) {
    if (asString(data.code) === "browser_remote_debugging_off") {
      return <BrowserRemoteDebuggingAlert address={asString(data.enable_url)} />;
    }
    return <ErrorView message={asString(data.error) || t("failed")} />;
  }
  if (Array.isArray(data.tabs)) {
    const tabs = asArray(data.tabs).map(asRecord);
    return (
      <FieldList>
        {tabs.map((tab, index) => (
          <InlineField
            key={asString(tab.tab) || index}
            label={asString(tab.title) || asString(tab.url) || String(index + 1)}
          >
            {asString(tab.url)}{tab.active ? ` · ${t("browserTabActive")}` : ""}
          </InlineField>
        ))}
      </FieldList>
    );
  }
  if (data.text != null) {
    return (
      <FieldList>
        {asString(data.title) && <InlineField label={t("title")}>{asString(data.title)}</InlineField>}
        {showUrl && <InlineField label={t("url")}>{resultUrl}</InlineField>}
        <Field label={t("browserPageText")}>
          <MonoBlock>{asString(data.text)}</MonoBlock>
        </Field>
      </FieldList>
    );
  }
  const elements = asArray(data.elements);
  if (data.count != null || elements.length > 0) {
    const advisory = asString(data.note) || asString(data.hint);
    return (
      <FieldList>
        {asString(data.did) && <InlineField label={t("computerResult")}>{asString(data.did)}</InlineField>}
        {asString(data.title) && <InlineField label={t("title")}>{asString(data.title)}</InlineField>}
        {showUrl && (
          <InlineField label={t("url")}>
            {resultUrl}{data.url_changed === true ? ` · ${t("browserUrlChanged")}` : ""}
          </InlineField>
        )}
        {data.count != null && <InlineField label={t("computerTotalElements")}>{asString(data.count)}</InlineField>}
        {data.dialog != null && (
          <InlineField label={t("browserDialog")}>
            {`${asString(asRecord(data.dialog).type)}: ${asString(asRecord(data.dialog).message)}`}
          </InlineField>
        )}
        {advisory && <EmptyHint>{advisory}</EmptyHint>}
        {elements.length > 0 && (
          <Field label={t("computerElements")}>
            <MonoBlock>{JSON.stringify(elements, null, 2)}</MonoBlock>
          </Field>
        )}
      </FieldList>
    );
  }
  const did = asString(data.did);
  if (did) {
    const note = asString(data.note);
    return (
      <FieldList>
        <InlineField label={t("computerResult")}>{did}</InlineField>
        {note && <EmptyHint>{note}</EmptyHint>}
      </FieldList>
    );
  }
  return null;
}

export function ToolResultView({ name, content, args }: { name: string; content: string; args?: Record<string, unknown> }) {
  const t = useTranslations("ToolViews");
  const parsed = tryParse(content);

  // MCP discovery results (the full server/tool catalog with JSON schemas) are
  // internal noise — the call card already conveys that discovery happened.
  if (name === "list_mcp_tools" || name === "list_mcp_resources") return null;

  // The task tools' result is a bare confirmation string that names raw "task-N"
  // ids (for the model); the call card already shows the tasks as #N, so don't
  // re-render the confirmation and leak the internal ids.
  if (name === "set_tasks" || name === "update_tasks") return null;

  // An open_artifact result is just compact model_context metadata — the rendered
  // artifact is the deliverable (it shows outside the card).
  if (name === "open_artifact") return null;

  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const data = parsed as Record<string, unknown>;
    const code = asString(data.code);
    // "scheduled" notices (web_search_started, bash_started, background_task_scheduled)
    // are transient implementation details — the matching *_completed result arrives
    // shortly and renders instead. Don't render the raw scheduling payload.
    if (code.endsWith("_started") || code === "background_task_scheduled") return null;
    if (code === "tool_error") return null;
    if (code === "web_search_completed") return <WebSearchResultView data={data} />;
    if (code === "web_search_error") return <ErrorView message={asString(data.message) || t("searchFailed")} />;
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
    if (name === "edit_file" || name === "write_file") return <FileEditResultView data={data} />;
    if (name === "fetch_url") return <FetchUrlResultView data={data} />;
    if (name === "load_skill") return <LoadSkillResultView data={data} />;
    if (name === "ask_user") return <AskUserResultView data={data} />;
    if (name === "computer") return <ComputerResultView data={data} />;
    if (name === "browser") return <BrowserResultView data={data} args={args} />;
    return <GenericView data={data} />;
  }

  // Non-JSON results (e.g. a spawned agent's final text) render as prose.
  return <MarkdownContent content={content} />;
}
