"use client";

import { Box, Flex, IconButton, Menu, Text } from "@chakra-ui/react";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { LuActivity, LuClock, LuFolder, LuMoveDownRight, LuPlus, LuServer, LuSquare, LuTerminal } from "react-icons/lu";
import { abortToolCall, deleteTerminal, fetchBackgroundJobs, listTerminals, sendToolToBackground, type BackgroundJob, type Location } from "@/lib/api";
import type { ChatMessage } from "@/lib/use-chat";
import type { ToolEventStatus } from "@/lib/tool-event";
import { ToolCall } from "./tool-call";
import { TerminalSurface } from "./terminal-panel";
import { Tooltip } from "./ui/tooltip";
import { PanelTab } from "./ui/panel-tab";
import { PanelCard, PanelHeader, PanelEmptyState } from "./ui/panel";
import { DropdownMenu } from "@/components/ui/menu";
import { SegmentedToggle } from "./ui/segmented-toggle";
import { InlineField } from "./ui/display";
import { scrollFade } from "@/lib/scroll-fade";
import { hasBackgroundTaskIdentifier } from "@/lib/tool-event";
import { locationTargetAddress, locationTargetLabel } from "./location-status";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { ActivityIcon, ActivitySpinner } from "./ui/activity-icon";
import { Pill } from "./ui/pill";

// A shell command surfaced from the transcript, carried in the exact shape the
// ToolCall component consumes so each row renders as a real tool call.
interface ShellTask {
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  status: ToolEventStatus;
  result: unknown;
  timestamp: string;
  running: boolean;
  canBackground: boolean;
  // Already detached (the model ran it with background=true, or the user pushed a
  // foreground command to the background).
  backgrounded: boolean;
}

function shellTasksFromMessages(messages: ChatMessage[]): ShellTask[] {
  const tasks: ShellTask[] = [];
  for (const message of messages) {
    if (message.role !== "tool_call" || message.content !== "bash") continue;
    const meta = message.meta ?? {};
    const status = String(meta.status ?? "completed") as ToolEventStatus;
    const running = status === "running" || status === "input_required";
    tasks.push({
      toolCallId: String(meta.toolCallId ?? message.id),
      name: message.content,
      arguments: (meta.arguments as Record<string, unknown> | undefined) ?? {},
      status,
      result: meta.result,
      timestamp: message.timestamp,
      running,
      canBackground: message.content === "bash",
      backgrounded: running && hasBackgroundTaskIdentifier(meta.result),
    });
  }
  // Newest first — the live tail of shell activity reads back in time.
  return tasks.sort((first, second) => second.timestamp.localeCompare(first.timestamp));
}

function startedResultForJob(job: BackgroundJob): Record<string, unknown> {
  if (job.kind === "bash") {
    return { code: "bash_started", task_identifier: job.job_id };
  }
  return { code: `${job.kind}_started`, task_identifier: job.job_id };
}

function shellTasksFromBackgroundJobs(jobs: BackgroundJob[]): ShellTask[] {
  return jobs.map((job) => ({
    toolCallId: job.tool_call_id || job.job_id,
    name: job.kind,
    arguments: job.arguments ?? {},
    status: "running" as ToolEventStatus,
    result: startedResultForJob(job),
    timestamp: job.started_at,
    running: true,
    canBackground: job.kind === "bash",
    backgrounded: job.detached,
  }));
}

function mergeTasks(messageTasks: ShellTask[], liveTasks: ShellTask[]): ShellTask[] {
  const tasksByIdentifier = new Map<string, ShellTask>();
  const liveTaskIdentifiers = new Set(liveTasks.map((task) => task.toolCallId));
  for (const task of messageTasks) {
    if (task.running && task.backgrounded && !liveTaskIdentifiers.has(task.toolCallId)) {
      continue;
    }
    tasksByIdentifier.set(task.toolCallId, task);
  }
  for (const task of liveTasks) {
    const messageTask = tasksByIdentifier.get(task.toolCallId);
    const transcriptJustification = messageTask?.arguments.justification;
    tasksByIdentifier.set(task.toolCallId, transcriptJustification && !task.arguments.justification
      ? { ...task, arguments: { ...task.arguments, justification: transcriptJustification } }
      : task);
  }
  return Array.from(tasksByIdentifier.values())
    .sort((first, second) => second.timestamp.localeCompare(first.timestamp));
}

// A running shell command: the tool card plus the actions that only make sense
// while it is live — pushing a still-blocking foreground command to the
// background, or stopping it outright.
function RunningTaskRow({ task, sessionId }: { task: ShellTask; sessionId: string | null }) {
  const translation = useTranslations("BackgroundTasksPanel");
  const [busy, setBusy] = useState<"stop" | "background" | null>(null);

  async function handleStop() {
    if (!sessionId) return;
    setBusy("stop");
    try {
      await abortToolCall(sessionId, task.toolCallId);
    } finally {
      setBusy(null);
    }
  }

  async function handleBackground() {
    if (!sessionId) return;
    setBusy("background");
    try {
      await sendToolToBackground(sessionId, task.toolCallId);
    } finally {
      setBusy(null);
    }
  }

  return (
    <ToolCall
      name={task.name}
      arguments={task.arguments}
      result={task.result}
      status={task.status}
      toolCallId={task.toolCallId}
      actions={
        <>
          {task.canBackground && !task.backgrounded && (
            <Tooltip content={translation("sendToBackgroundHint")} openDelay={300}>
              <IconButton
                aria-label={busy === "background" ? translation("sending") : translation("sendToBackground")}
                variant="plain"
                colorPalette="blue"
                boxSize="5"
                minW="5"
                disabled={!sessionId || busy !== null}
                onClick={handleBackground}
              >
                {busy === "background" ? <ActivitySpinner /> : <ActivityIcon><LuMoveDownRight /></ActivityIcon>}
              </IconButton>
            </Tooltip>
          )}
          <Tooltip content={translation("stop")} openDelay={300}>
            <IconButton
              aria-label={busy === "stop" ? translation("stopping") : translation("stop")}
              variant="plain"
              colorPalette="red"
              boxSize="5"
              minW="5"
              disabled={!sessionId || busy !== null}
              onClick={handleStop}
            >
              {busy === "stop" ? <ActivitySpinner /> : <ActivityIcon><LuSquare /></ActivityIcon>}
            </IconButton>
          </Tooltip>
        </>
      }
    />
  );
}

// A fresh, unique terminal key. Module-level (not called during render) so the
// impurity of Date.now/Math.random stays out of the component body.
function newTerminalKey(): string {
  return `terminal-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

export function BackgroundTasksPanel({
  onClose,
  messages,
  sessionId,
  workingDirectory,
  locations = [],
}: {
  open: boolean;
  onClose: () => void;
  messages: ChatMessage[];
  sessionId: string | null;
  workingDirectory: string;
  locations?: Location[];
}) {
  const translation = useTranslations("BackgroundTasksPanel");
  const tasks = useMemo(() => shellTasksFromMessages(messages), [messages]);
  const [backgroundJobs, setBackgroundJobs] = useState<BackgroundJob[]>([]);
  const [activeView, setActiveView] = useState<"terminal" | "processes">("terminal");
  // The set of terminals for this session's context, and which one is on top. Restored
  // from the server on mount/context change so tabs survive reloads. "main" is the
  // canonical key for the first terminal created in an empty context.
  const [terminals, setTerminals] = useState<string[]>(["main"]);
  const [activeTerminal, setActiveTerminal] = useState<string>("main");
  // The location each terminal targets (by id); defaults to the project's first location.
  const [terminalLocations, setTerminalLocations] = useState<Record<string, string>>({});
  const locationForTerminal = (key: string): Location | undefined => {
    const chosen = locations.find((location) => location.id === terminalLocations[key]);
    return chosen ?? locations[0];
  };
  const liveTasks = useMemo(() => shellTasksFromBackgroundJobs(backgroundJobs), [backgroundJobs]);
  const mergedTasks = useMemo(() => mergeTasks(tasks, liveTasks), [tasks, liveTasks]);
  const running = mergedTasks.filter((task) => task.running);
  const completed = mergedTasks.filter((task) => !task.running);

  useEffect(() => {
    let cancelled = false;

    async function refreshBackgroundJobs() {
      if (!sessionId) {
        setBackgroundJobs([]);
        return;
      }
      const jobs = await fetchBackgroundJobs(sessionId);
      if (!cancelled) setBackgroundJobs(jobs);
    }

    void refreshBackgroundJobs();
    const interval = window.setInterval(() => void refreshBackgroundJobs(), 1500);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [sessionId]);

  // Restore the context's terminals whenever the session/working directory changes.
  useEffect(() => {
    let cancelled = false;
    async function loadTerminals() {
      const infos = await listTerminals(sessionId, workingDirectory);
      if (cancelled) return;
      const keys = infos.map((info) => info.terminalKey);
      if (keys.length === 0) keys.push("main");
      setTerminals(keys);
      setActiveTerminal((current) => (keys.includes(current) ? current : keys[0]));
    }
    void loadTerminals();
    return () => {
      cancelled = true;
    };
  }, [sessionId, workingDirectory]);

  // A terminal's environment is chosen when it's created (via the "＋" menu when there is
  // more than one location) and fixed for its life — a shell on host A and a shell on
  // host B are simply different terminals, so the location is not changed mid-session.
  function addTerminal(locationId?: string) {
    const key = newTerminalKey();
    setTerminals((current) => [...current, key]);
    setActiveTerminal(key);
    if (locationId) setTerminalLocations((current) => ({ ...current, [key]: locationId }));
  }

  function closeTerminal(key: string) {
    const index = terminals.indexOf(key);
    const next = terminals.filter((terminalKey) => terminalKey !== key);
    void deleteTerminal(sessionId, workingDirectory, key);
    // Never leave the panel with no terminal: closing the last one opens a fresh one.
    if (next.length === 0) {
      const fresh = newTerminalKey();
      setTerminals([fresh]);
      setActiveTerminal(fresh);
      return;
    }
    setTerminals(next);
    if (activeTerminal === key) {
      setActiveTerminal(next[Math.max(0, index - 1)] ?? next[0]);
    }
  }

  return (
    <PanelCard>
      <PanelHeader
        icon={activeView === "terminal" ? <LuTerminal size={14} /> : <LuActivity size={14} />}
        title={activeView === "terminal" ? translation("terminal") : translation("backgroundProcesses")}
        onClose={onClose}
        closeLabel={translation("collapseSidebar")}
      >
        <SegmentedToggle
          value={activeView}
          onChange={setActiveView}
          options={[
            { value: "terminal", label: translation("terminal"), icon: <LuTerminal size={14} /> },
            { value: "processes", label: translation("processes"), icon: <LuActivity size={14} /> },
          ]}
        />
      </PanelHeader>

      <Box flex={1} minH={0} position="relative" overflow="hidden">
        <Flex position="absolute" inset={0} direction="column" visibility={activeView === "terminal" ? "visible" : "hidden"}>
          {/* Terminal tabs — the shared PanelTab (identical to the Artifacts panel's tabs),
              plus a "＋" to spawn a new terminal and the location switcher, all at one height. */}
          <Flex px={4} py={2} overflowX="auto" flexShrink={0}>
            <Flex gap={1.5} align="center">
              {terminals.map((key, index) => {
                const terminalLocation = locationForTerminal(key);
                const tabTooltip = (
                  <Box fontSize="xs" lineHeight="1.6" maxW="300px">
                    <Text fontWeight="semibold" mb={terminalLocation ? 1 : 0} color="fg">{translation("terminalNumber", { number: index + 1 })}</Text>
                    {terminalLocation ? (
                      <Flex direction="column" gap={1}>
                        <InlineField label={translation("location")}><Text>{locationTargetLabel(terminalLocation)}</Text></InlineField>
                        <InlineField label={translation("type")}><Text>{terminalLocation.kind === "remote" ? translation("remoteSsh") : translation("local")}</Text></InlineField>
                        <Text color="fg.muted" wordBreak="break-all" mt={0.5}>{locationTargetAddress(terminalLocation)}</Text>
                      </Flex>
                    ) : null}
                  </Box>
                );
                return (
                  <PanelTab
                    key={key}
                    icon={<LuTerminal size={13} />}
                    label={translation("terminalNumber", { number: index + 1 })}
                    active={key === activeTerminal}
                    onSelect={() => setActiveTerminal(key)}
                    onClose={() => closeTerminal(key)}
                    tooltip={tabTooltip}
                    closeLabel={translation("closeTerminalNumber", { number: index + 1 })}
                  />
                );
              })}
              {locations.length > 1 ? (
                // Multiple environments: "＋" opens a menu to pick where the new terminal runs.
                <DropdownMenu
                  trigger={
                    <IconButton aria-label={translation("newTerminal")} title={translation("newTerminal")} variant="ghost" flexShrink={0}>
                      <LuPlus size={14} />
                    </IconButton>
                  }
                  minW="200px"
                >
                  <Text px={2} py={1} textStyle="sectionLabel">{translation("newTerminalIn")}</Text>
                  {locations.map((location) => (
                    <Menu.Item key={location.id} value={location.id} onClick={() => addTerminal(location.id)}>
                      {location.kind === "remote" ? <LuServer size={14} /> : <LuFolder size={14} />}
                      <Box flex={1}>{locationTargetLabel(location)}</Box>
                    </Menu.Item>
                  ))}
                </DropdownMenu>
              ) : (
                <Tooltip content={translation("newTerminal")} openDelay={300}>
                  <IconButton aria-label={translation("newTerminal")} variant="ghost" flexShrink={0} onClick={() => addTerminal()}>
                    <LuPlus size={14} />
                  </IconButton>
                </Tooltip>
              )}
            </Flex>
          </Flex>
          {/* Every terminal stays mounted so switching tabs never drops a live shell;
              only the active one is visible. */}
          <Box position="relative" flex={1} minH={0}>
            {terminals.map((key) => {
              const terminalLocation = locationForTerminal(key);
              return (
              <Box key={key} position="absolute" inset={0} visibility={activeView === "terminal" && key === activeTerminal ? "visible" : "hidden"}>
                <TerminalSurface
                  sessionId={sessionId}
                  workingDirectory={workingDirectory}
                  terminalKey={key}
                  location={terminalLocation ? { kind: terminalLocation.kind, base_directory: terminalLocation.base_directory, host_alias: terminalLocation.host_alias } : undefined}
                />
              </Box>
              );
            })}
          </Box>
        </Flex>
        <Box
          position="absolute"
          inset={0}
          display={activeView === "processes" ? "block" : "none"}
          overflowY="auto"
          px={4}
          py={2}
          css={scrollFade}
        >
          {running.length === 0 && completed.length === 0 ? (
            <PanelEmptyState
              icon={<LuTerminal />}
              title={translation("noProcessesTitle")}
              description={translation("noProcessesDescription")}
            />
          ) : (
            <Flex direction="column" gap={2}>
              {running.length > 0 && (
                <DisclosureRow
                  defaultOpen
                  tone="active"
                  maxH="360px"
                  followTailKey={running.length}
                  icon={<LuActivity />}
                  title={<DisclosureLabel shimmer>{translation("processesActive")}</DisclosureLabel>}
                  badges={
                    <Pill
                      colorPalette="blue"
                      icon={<ActivitySpinner />}
                    >
                      {running.length}
                    </Pill>
                  }
                >
                  <Flex direction="column" gap={1}>
                    {running.map((task) => (
                      <RunningTaskRow key={task.toolCallId} task={task} sessionId={sessionId} />
                    ))}
                  </Flex>
                </DisclosureRow>
              )}

              {completed.length > 0 && (
                <DisclosureRow
                  maxH="min(52vh, 480px)"
                  icon={<LuClock />}
                  title={<DisclosureLabel>{translation("processesTerminated")}</DisclosureLabel>}
                  badges={<Pill colorPalette="gray">{completed.length}</Pill>}
                >
                  <Flex direction="column" gap={2}>
                    {completed.map((task) => (
                      <ToolCall
                        key={task.toolCallId}
                        name={task.name}
                        arguments={task.arguments}
                        result={task.result}
                        status={task.status}
                        toolCallId={task.toolCallId}
                      />
                    ))}
                  </Flex>
                </DisclosureRow>
              )}
            </Flex>
          )}
        </Box>
      </Box>
    </PanelCard>
  );
}
