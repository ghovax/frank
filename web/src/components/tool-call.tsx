"use client";

import { Badge, Box, Button, Flex, HStack, Input, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { LuCheck } from "react-icons/lu";
import { getToolCallDisplay } from "@/lib/tool-display";
import type { PermissionDecision, QuestionAnswer, ToolEvent, ToolPermission, ToolQuestion } from "@/lib/tool-event";
import { ToolCallView, ToolResultView, extractToolArtifacts } from "./tool-views";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolRiskBadges, ToolStatusBadge } from "./tool-card";

interface ToolCallProps extends ToolEvent {
  agents?: { id: string; name: string; title?: string }[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  // The single live web-preview id (owned by ChatPanel). Only the matching
  // iframe-type artifact mounts its frame; the rest collapse to a placeholder.
  activePreviewId?: string | null;
  onActivatePreview?: (id: string) => void;
}

// The human-in-the-loop approval, rendered inside the tool card it belongs to,
// only while pending. Deny sits far left and Allow once far right (so they can't
// be mis-clicked), with Always allow beside Allow once. Keys: 1 deny, 2 allow
// always, 3 allow once. Once decided, the card's own status badge (Running →
// Completed, or Failed) carries the outcome — the prompt disappears entirely.
function ToolPermissionPrompt({
  permission,
  onPermission,
}: {
  permission: ToolPermission;
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);

  function decide(decision: PermissionDecision) {
    onPermission?.(permission.requestId, decision);
  }

  function handleKeyDown(event: React.KeyboardEvent) {
    // 1 deny, 2 allow always, 3 allow once — mirrors the on-screen buttons.
    if (event.key === "1") {
      event.preventDefault();
      decide("deny");
    } else if (event.key === "2") {
      event.preventDefault();
      decide("allow_always");
    } else if (event.key === "3") {
      event.preventDefault();
      decide("allow_once");
    }
  }

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  // Flat — the yellow card header already signals the approval, and the command
  // is shown above, so the prompt is just the controls (no nested box).
  return (
    <Flex
      ref={boxRef}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      align="center"
      justify="space-between"
      gap={2}
      _focus={{ outline: "none", boxShadow: "none" }}
    >
      <Button size="xs" colorPalette="red" variant="solid" onClick={() => decide("deny")}>
        Deny (1)
      </Button>
      <HStack gap={2}>
        <Button size="xs" colorPalette="blue" variant="subtle" onClick={() => decide("allow_always")}>
          Always allow (2)
        </Button>
        <Button size="xs" colorPalette="green" variant="solid" onClick={() => decide("allow_once")}>
          Allow once (3)
        </Button>
      </HStack>
    </Flex>
  );
}

// An ask_user prompt: one or more questions, each with selectable options and an
// optional "type your own answer" field. Lives inside the tool card while
// pending (status "input_required"), same lifecycle as a permission. Custom text
// overrides any option selection for that question; Submit sends one answer per
// question (in order) and hands the card back to its running lifecycle.
function ToolQuestionPrompt({
  question,
  onQuestion,
}: {
  question: ToolQuestion;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
}) {
  const items = question.questions;
  const [selected, setSelected] = useState<Record<number, string[]>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  function toggle(index: number, label: string, multiple: boolean) {
    setSelected((current) => {
      const active = current[index] ?? [];
      if (!multiple) {
        return { ...current, [index]: active.length === 1 && active[0] === label ? [] : [label] };
      }
      return {
        ...current,
        [index]: active.includes(label) ? active.filter((value) => value !== label) : [...active, label],
      };
    });
  }

  function submit() {
    const answers: QuestionAnswer[] = items.map((item, index) => {
      const text = (custom[index] ?? "").trim();
      if (text) return text;
      const chosen = selected[index] ?? [];
      return item.multiple ? chosen : (chosen[0] ?? "");
    });
    onQuestion?.(question.requestId, answers);
  }

  return (
    <Flex ref={boxRef} tabIndex={0} direction="column" gap={3} _focus={{ outline: "none", boxShadow: "none" }}>
      {items.map((item, index) => {
        const multiple = !!item.multiple;
        const customEnabled = item.custom !== false;
        const hasOptions = !!item.options && item.options.length > 0;
        const active = selected[index] ?? [];
        const text = custom[index] ?? "";
        return (
          <Flex key={index} direction="column" gap={1.5}>
            {item.header ? (
              <Text fontSize="xs" fontWeight="semibold">
                {item.header}
              </Text>
            ) : null}
            <Text fontSize="sm">{item.question}</Text>
            {hasOptions ? (
              <Flex direction="column" gap={1}>
                {item.options!.map((option) => {
                  const isSelected = !text && active.includes(option.label);
                  return (
                    <Flex
                      key={option.label}
                      as="button"
                      align="center"
                      gap={2}
                      px={2.5}
                      py={2}
                      borderRadius="sm"
                      border="1px solid"
                      borderColor={isSelected ? "blue.solid" : "border"}
                      bg={isSelected ? "blue.subtle" : "bg"}
                      cursor="pointer"
                      textAlign="left"
                      transition="all 120ms"
                      _hover={{ borderColor: isSelected ? "blue.solid" : "border.emphasized" }}
                      onClick={() => { if (!text) toggle(index, option.label, multiple); }}
                      title={option.description}
                    >
                      <Box
                        w="14px" h="14px" flexShrink={0}
                        borderRadius={multiple ? "sm" : "full"}
                        border="2px solid"
                        borderColor={isSelected ? "blue.solid" : "border"}
                        bg={isSelected ? "blue.solid" : "transparent"}
                        display="flex" alignItems="center" justifyContent="center"
                      >
                        {isSelected && (
                          <Box color="white" display="flex" alignItems="center" justifyContent="center" fontSize="10px">
                            <LuCheck size={11} />
                          </Box>
                        )}
                      </Box>
                      <Flex direction="column" minW={0} flex={1}>
                        <Text fontSize="sm" fontWeight="medium" color={isSelected ? "fg" : "fg.emphasized"}>
                          {option.label}
                        </Text>
                        {option.description && (
                          <Text fontSize="xs" color="fg.subtle" lineClamp={2}>
                            {option.description}
                          </Text>
                        )}
                      </Flex>
                    </Flex>
                  );
                })}
              </Flex>
            ) : null}
            {customEnabled ? (
              <Input
                size="xs"
                placeholder="Type your own answer"
                value={text}
                onChange={(event) => setCustom((current) => ({ ...current, [index]: event.target.value }))}
              />
            ) : null}
          </Flex>
        );
      })}
      <Flex justify="flex-end">
        <Button size="xs" colorPalette="green" variant="solid" onClick={submit}>
          Submit
        </Button>
      </Flex>
    </Flex>
  );
}

function isToolErrorResult(content: string | null): boolean {
  if (!content) return false;
  try {
    const parsed = JSON.parse(content);
    return !!parsed && typeof parsed === "object" && !Array.isArray(parsed) && (parsed as Record<string, unknown>).code === "tool_error";
  } catch {
    return false;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function isBackgroundResult(result: unknown): boolean {
  const code = String(asRecord(result).code ?? "");
  return code.endsWith("_started") || code === "background_task_scheduled";
}

export function ToolCall({ name, arguments: toolArguments, result, status, permission, question, agents = [], onPermission, onQuestion }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const hasArguments = !!toolArguments && Object.keys(toolArguments).length > 0;
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // Renderable artifacts (e.g. a map) render outside the card and stay visible;
  // the textual result stays inside the collapsible body. When the result is an
  // artifact, there is no separate text to show inside.
  const artifacts = resultContent ? extractToolArtifacts(name, resultContent) : [];
  const showResultInside = resultContent != null && artifacts.length === 0 && !isToolErrorResult(resultContent);
  // Only while pending — once decided, the status badge carries the outcome.
  const showPermission = !!permission && status === "input_required";
  const showQuestion = !!question && status === "input_required";
  const collapsible = hasArguments || showResultInside || showPermission || showQuestion;
  // A pending prompt forces the card open so the controls are visible without a click.
  const bodyOpen = open || status === "input_required";
  const background = status === "running" && isBackgroundResult(result);

  const { icon: Icon, iconColor, label } = getToolCallDisplay(name, toolArguments);

  return (
    <Flex direction="column" gap={1.5} align="stretch">
      <ToolCard>
        <ToolCardHeader
          icon={
            <Box color={iconColor}>
              <Icon size={12} />
            </Box>
          }
          title={label}
          badges={
            <>
              <ToolRiskBadges arguments={toolArguments} />
              {status === "running" || status === "completed" || status === "failed" || status === "input_required" ? <ToolStatusBadge status={status} /> : null}
              {background ? <Badge size="sm" variant="subtle" colorPalette="purple" borderRadius="sm" flexShrink={0}>Background</Badge> : null}
            </>
          }
          open={bodyOpen}
          collapsible={collapsible}
          shimmer={status === "running"}
          headerBg={status === "input_required" ? "yellow.subtle" : undefined}
          onToggle={() => setOpen((current) => !current)}
        />

        <AnimatePresence initial={false}>
          {collapsible && bodyOpen && (
            <motion.div
              key="body"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
              style={{ overflow: "hidden" }}
            >
              <ToolCardBody maxH="560px">
                {/* gap matches FieldList's own field spacing so the call's last field
                    (e.g. Risk) and the result's first (e.g. PID) read as one list. */}
                <Flex direction="column" gap={2} align="stretch">
                  {hasArguments && <ToolCallView name={name} args={toolArguments} agents={agents} />}
                  {showPermission && permission && <ToolPermissionPrompt permission={permission} onPermission={onPermission} />}
                  {showQuestion && question && <ToolQuestionPrompt question={question} onQuestion={onQuestion} />}
                  {showResultInside && <ToolResultView name={name} content={resultContent ?? ""} />}
                </Flex>
              </ToolCardBody>
            </motion.div>
          )}
        </AnimatePresence>
      </ToolCard>
    </Flex>
  );
}
