"use client";

import { Box, Button, EmptyState, Flex, IconButton, Text, VStack } from "@chakra-ui/react";
import { useMemo, useState, type PointerEvent } from "react";
import { LuChevronDown, LuChevronRight, LuMoveDownRight, LuSquare, LuTerminal, LuX } from "react-icons/lu";
import { motion } from "motion/react";
import { abortToolCall, sendToolToBackground } from "@/lib/api";
import type { ChatMessage } from "@/lib/use-chat";
import type { ToolEventStatus } from "@/lib/tool-event";
import { ToolCall } from "./tool-call";

// A Chakra Flex that is also a motion component, matching the agents/preview
// sidebars so this panel slides in and out the same way.
const MotionFlex = motion.create(Flex);

// A shell command surfaced from the transcript, carried in the exact shape the
// ToolCall component consumes so each row renders as a real tool call.
interface ShellTask {
  toolCallId: string;
  arguments: Record<string, unknown>;
  status: ToolEventStatus;
  result: unknown;
  timestamp: string;
  running: boolean;
  // Already detached (the model ran it with background=true, or the user pushed a
  // foreground command to the background) — its result is a "*_started" placeholder.
  backgrounded: boolean;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function isBackgroundResult(result: unknown): boolean {
  const code = String(asRecord(result).code ?? "");
  return code.endsWith("_started") || code === "background_task_scheduled";
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
      arguments: (meta.arguments as Record<string, unknown> | undefined) ?? {},
      status,
      result: meta.result,
      timestamp: message.timestamp,
      running,
      backgrounded: running && isBackgroundResult(meta.result),
    });
  }
  // Newest first — the live tail of shell activity reads back in time.
  return tasks.sort((first, second) => second.timestamp.localeCompare(first.timestamp));
}

// A running shell command: the tool card plus the actions that only make sense
// while it is live — pushing a still-blocking foreground command to the
// background, or stopping it outright.
function RunningTaskRow({ task, sessionId }: { task: ShellTask; sessionId: string | null }) {
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
    <Box>
      <ToolCall
        name="bash"
        arguments={task.arguments}
        result={task.result}
        status={task.status}
        toolCallId={task.toolCallId}
      />
      <Flex gap={1.5} justify="flex-end" mt={1}>
        {!task.backgrounded && (
          <Button
            size="xs"
            variant="outline"
            borderRadius="sm"
            h="24px"
            px={1.5}
            flexShrink={0}
            disabled={!sessionId || busy !== null}
            loading={busy === "background"}
            onClick={handleBackground}
            title="Let this command keep running in the background and continue the turn"
          >
            <LuMoveDownRight size={11} />
            Send to background
          </Button>
        )}
        <Button
          size="xs"
          variant="outline"
          colorPalette="red"
          borderRadius="sm"
          h="24px"
          px={1.5}
          flexShrink={0}
          disabled={!sessionId || busy !== null}
          loading={busy === "stop"}
          onClick={handleStop}
        >
          <LuSquare size={11} />
          Stop
        </Button>
      </Flex>
    </Box>
  );
}

export function BackgroundTasksPanel({
  onClose,
  messages,
  sessionId,
  width,
  onResizeStart,
}: {
  open: boolean;
  onClose: () => void;
  messages: ChatMessage[];
  sessionId: string | null;
  width: number;
  onResizeStart: (event: PointerEvent<HTMLDivElement>) => void;
}) {
  const tasks = useMemo(() => shellTasksFromMessages(messages), [messages]);
  const running = tasks.filter((task) => task.running);
  const completed = tasks.filter((task) => !task.running);
  // Finished commands are tucked away so they don't clutter the live view; a
  // toggle reveals the session's full shell history when it is wanted.
  const [showCompleted, setShowCompleted] = useState(false);

  return (
    <MotionFlex
      w={{ base: "100%", md: `${width}px` }}
      maxW={{ base: "100%", md: "52vw" }}
      minW={{ base: "100%", md: "300px" }}
      h={{ base: "100dvh", md: "auto" }}
      direction="column"
      bg="bg"
      borderLeft="1px solid"
      borderColor="border"
      flexShrink={0}
      position={{ base: "fixed", md: "relative" }}
      inset={{ base: 0, md: "auto" }}
      zIndex={{ base: 1000, md: "auto" }}
      minH={0}
      display="flex"
      initial={{ opacity: 0, x: 24 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 24 }}
      transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
    >
      <Box
        display={{ base: "none", md: "block" }}
        position="absolute"
        top={0}
        bottom={0}
        left="-4px"
        w="8px"
        cursor="col-resize"
        zIndex={1}
        onPointerDown={onResizeStart}
      />
      <Flex align="center" gap={2} px={3} py={2} borderBottom="1px solid" borderColor="border" flexShrink={0}>
        <Box color="fg.muted"><LuTerminal size={15} /></Box>
        <Text fontSize="sm" fontWeight="bold" flex={1}>Background processes</Text>
        <IconButton aria-label="Collapse background processes sidebar" size="xs" variant="ghost" borderRadius="sm" onClick={onClose}>
          <LuX size={15} />
        </IconButton>
      </Flex>

      <Box flex={1} minH={0} overflowY="auto" px={3} py={3}>
        {running.length === 0 && completed.length === 0 ? (
          <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2} pt={4} pb={12}>
            <EmptyState.Root>
              <EmptyState.Content>
                <EmptyState.Indicator>
                  <LuTerminal />
                </EmptyState.Indicator>
                <VStack gap={1}>
                  <EmptyState.Title>No background processes</EmptyState.Title>
                  <EmptyState.Description>
                    Shell commands running will appear here
                  </EmptyState.Description>
                </VStack>
              </EmptyState.Content>
            </EmptyState.Root>
          </Flex>
        ) : (
          <Flex direction="column" gap={2}>
            {running.length === 0 && (
              <EmptyState.Root size="sm">
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuTerminal />
                  </EmptyState.Indicator>
                  <VStack gap={0}>
                    <EmptyState.Title fontSize="sm">No active processes</EmptyState.Title>
                    <EmptyState.Description fontSize="xs">
                      All shell commands have finished
                    </EmptyState.Description>
                  </VStack>
                </EmptyState.Content>
              </EmptyState.Root>
            )}
            {running.length > 0 && running.map((task) => (
              <RunningTaskRow key={task.toolCallId} task={task} sessionId={sessionId} />
            ))}

            {completed.length > 0 && (
              <Box mt={running.length > 0 ? 2 : 0}>
                <Button
                  size="xs"
                  variant="ghost"
                  borderRadius="sm"
                  w="100%"
                  justifyContent="flex-start"
                  color="fg.muted"
                  fontWeight="medium"
                  onClick={() => setShowCompleted((current) => !current)}
                >
                  {showCompleted ? <LuChevronDown size={13} /> : <LuChevronRight size={13} />}
                  Processes terminated ({completed.length})
                </Button>
                {showCompleted && (
                  <Flex direction="column" gap={2} mt={2}>
                    {completed.map((task) => (
                      <ToolCall
                        key={task.toolCallId}
                        name="bash"
                        arguments={task.arguments}
                        result={task.result}
                        status={task.status}
                        toolCallId={task.toolCallId}
                      />
                    ))}
                  </Flex>
                )}
              </Box>
            )}
          </Flex>
        )}
      </Box>
    </MotionFlex>
  );
}
