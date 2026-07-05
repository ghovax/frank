"use client";

// A prominent overlay that appears above the chat input when the agent asks the
// user a question. Replaces the inline tool-card rendering so the user cannot
// miss it. Renders one question at a time with navigation between multiple
// questions, and blocks chat input until all questions are answered. Each
// question can be skipped individually, and the whole prompt can be dismissed
// (a decline) — which tells the model the user chose not to answer and stops.

import { Box, Button, Flex, IconButton, Input, Text } from "@chakra-ui/react";
import { AnimatePresence, motion } from "motion/react";
import { useRef, useState } from "react";
import { LuCheck, LuChevronLeft, LuChevronRight, LuSkipForward, LuX } from "react-icons/lu";
import type { QuestionAnswer, ToolQuestion } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";

interface QuestionOverlayProps {
  question: ToolQuestion;
  onQuestion: (requestId: string, answers: QuestionAnswer[]) => void;
  // Dismiss the whole prompt without answering (a decline). Undefined hides the
  // close affordance.
  onDismiss?: (requestId: string) => void;
}

export function QuestionOverlay({ question, onQuestion, onDismiss }: QuestionOverlayProps) {
  const items = question.questions;
  const [current, setCurrent] = useState(0);
  const [selected, setSelected] = useState<Record<number, string[]>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const [skipped, setSkipped] = useState<Record<number, boolean>>({});
  const boxRef = useRef<HTMLDivElement>(null);

  const total = items.length;
  const item = items[current];

  function toggle(index: number, label: string, multiple: boolean) {
    setSkipped((prev) => (prev[index] ? { ...prev, [index]: false } : prev));
    setSelected((prev) => {
      const active = prev[index] ?? [];
      if (!multiple) {
        return { ...prev, [index]: active.length === 1 && active[0] === label ? [] : [label] };
      }
      return {
        ...prev,
        [index]: active.includes(label) ? active.filter((v) => v !== label) : [...active, label],
      };
    });
  }

  // The answer for one question: its typed custom text, else the selected
  // label(s), else empty. A skipped question always resolves to an empty answer
  // so the model sees it was deliberately left unanswered.
  function answerFor(index: number): QuestionAnswer {
    if (skipped[index]) return items[index].multiple ? [] : "";
    const text = (custom[index] ?? "").trim();
    if (text) return text;
    const chosen = selected[index] ?? [];
    return items[index].multiple ? chosen : (chosen[0] ?? "");
  }

  function submit() {
    onQuestion(question.requestId, items.map((_, index) => answerFor(index)));
  }

  function skipCurrent() {
    setSkipped((prev) => ({ ...prev, [current]: true }));
    setSelected((prev) => ({ ...prev, [current]: [] }));
    setCustom((prev) => ({ ...prev, [current]: "" }));
    if (current < total - 1) {
      setCurrent((c) => c + 1);
    } else {
      // Last question skipped — submit with everything gathered so far.
      onQuestion(
        question.requestId,
        items.map((_, index) => (index === current ? (items[index].multiple ? [] : "") : answerFor(index)))
      );
    }
  }

  const multiple = !!item.multiple;
  const customEnabled = item.custom !== false;
  const hasOptions = !!item.options && item.options.length > 0;
  const active = selected[current] ?? [];
  const text = custom[current] ?? "";
  const isSkipped = !!skipped[current];

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: 8 }}
        transition={{ duration: 0.15 }}
      >
        <Box
          ref={boxRef}
          mx={2}
          mb={2}
          p={3}
          borderRadius="md"
          border="1px solid"
          borderColor="blue.solid"
          bg="bg"
          boxShadow="lg"
          maxH="50vh"
          overflowY="auto"
        >
          <Flex align="center" justify="space-between" mb={2} gap={2}>
            <Text fontSize="xs" fontWeight="bold" color="blue.fg">
              Question {current + 1} of {total}
            </Text>
            <Flex align="center" gap={1}>
              {total > 1 && (
                <>
                  <Button
                    size="xs"
                    variant="ghost"
                    borderRadius="sm"
                    disabled={current === 0}
                    onClick={() => setCurrent((c) => c - 1)}
                  >
                    <LuChevronLeft size={12} />
                  </Button>
                  <Button
                    size="xs"
                    variant="ghost"
                    borderRadius="sm"
                    disabled={current === total - 1}
                    onClick={() => setCurrent((c) => c + 1)}
                  >
                    <LuChevronRight size={12} />
                  </Button>
                </>
              )}
              {onDismiss && (
                <IconButton
                  aria-label="Dismiss without answering"
                  title="Dismiss without answering — the agent is told you declined and stops here"
                  size="xs"
                  variant="ghost"
                  borderRadius="sm"
                  color="fg.subtle"
                  onClick={() => onDismiss(question.requestId)}
                >
                  <LuX size={13} />
                </IconButton>
              )}
            </Flex>
          </Flex>

          <Flex direction="column" gap={2.5}>
            <MarkdownContent content={item.question} fontSize="sm" />

            {hasOptions && (
              <Flex direction="column" gap={1}>
                {item.options!.map((option) => {
                  const isSelected = !text && !isSkipped && active.includes(option.label);
                  return (
                    <Flex
                      key={option.label}
                      as="button"
                      align="center"
                      gap={2}
                      px={2.5}
                      py={1.5}
                      borderRadius="sm"
                      border="1px solid"
                      borderColor={isSelected ? "blue.solid" : "border"}
                      bg={isSelected ? "blue.subtle" : "bg"}
                      cursor="pointer"
                      textAlign="left"
                      transition="all 120ms"
                      _hover={{ borderColor: isSelected ? "blue.solid" : "border.emphasized" }}
                      onClick={() => { if (!text) toggle(current, option.label, multiple); }}
                      title={option.description}
                    >
                      <Box
                        w="16px" h="16px" flexShrink={0}
                        borderRadius={multiple ? "sm" : "full"}
                        border="2px solid"
                        borderColor={isSelected ? "blue.solid" : "border"}
                        bg={isSelected ? "blue.solid" : "transparent"}
                        display="flex" alignItems="center" justifyContent="center"
                      >
                        {isSelected && (
                          <LuCheck size={10} color="white" strokeWidth={3} />
                        )}
                      </Box>
                      <Flex direction="column" minW={0} flex={1}>
                        <MarkdownContent content={option.label} fontSize="sm" />
                        {option.description && (
                          <Box color="fg.muted">
                            <MarkdownContent content={option.description} fontSize="xs" />
                          </Box>
                        )}
                      </Flex>
                    </Flex>
                  );
                })}
              </Flex>
            )}

            {customEnabled && (
              <Input
                size="sm"
                placeholder="Type your own answer"
                value={text}
                onChange={(e) => {
                  const value = e.target.value;
                  if (value) setSkipped((prev) => (prev[current] ? { ...prev, [current]: false } : prev));
                  setCustom((prev) => ({ ...prev, [current]: value }));
                }}
              />
            )}

            {isSkipped && (
              <Text fontSize="xs" color="fg.subtle">
                Skipped — this question will be answered as blank.
              </Text>
            )}
          </Flex>

          <Flex justify="space-between" align="center" mt={3} gap={2}>
            <Button size="xs" variant="ghost" colorPalette="gray" borderRadius="sm" onClick={skipCurrent}>
              <LuSkipForward size={12} />
              {current < total - 1 ? "Skip" : "Skip & submit"}
            </Button>
            <Button size="xs" colorPalette="green" variant="solid" onClick={submit}>
              Submit
            </Button>
          </Flex>
        </Box>
      </motion.div>
    </AnimatePresence>
  );
}
