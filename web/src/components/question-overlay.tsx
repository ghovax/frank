"use client";

// A prominent overlay that appears above the chat input when the agent asks the
// user a question. Replaces the inline tool-card rendering so the user cannot
// miss it. Renders one question at a time with navigation between multiple
// questions, and blocks chat input until all questions are answered.

import { Box, Button, Flex, Input, Text } from "@chakra-ui/react";
import { AnimatePresence, motion } from "motion/react";
import { useRef, useState } from "react";
import { LuCheck, LuChevronLeft, LuChevronRight } from "react-icons/lu";
import type { QuestionAnswer, ToolQuestion } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";

interface QuestionOverlayProps {
  question: ToolQuestion;
  onQuestion: (requestId: string, answers: QuestionAnswer[]) => void;
}

export function QuestionOverlay({ question, onQuestion }: QuestionOverlayProps) {
  const items = question.questions;
  const [current, setCurrent] = useState(0);
  const [selected, setSelected] = useState<Record<number, string[]>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const boxRef = useRef<HTMLDivElement>(null);

  const total = items.length;
  const item = items[current];

  function toggle(index: number, label: string, multiple: boolean) {
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

  function submit() {
    const answers: QuestionAnswer[] = items.map((question, index) => {
      const text = (custom[index] ?? "").trim();
      if (text) return text;
      const chosen = selected[index] ?? [];
      return question.multiple ? chosen : (chosen[0] ?? "");
    });
    onQuestion(question.requestId, answers);
  }

  const multiple = !!item.multiple;
  const customEnabled = item.custom !== false;
  const hasOptions = !!item.options && item.options.length > 0;
  const active = selected[current] ?? [];
  const text = custom[current] ?? "";

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
          <Flex align="center" justify="space-between" mb={2}>
            <Text fontSize="xs" fontWeight="bold" color="blue.fg">
              Question {current + 1} of {total}
            </Text>
            {total > 1 && (
              <Flex align="center" gap={1}>
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
              </Flex>
            )}
          </Flex>

          <Flex direction="column" gap={2.5}>
            <MarkdownContent content={item.question} fontSize="sm" />

            {hasOptions && (
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
                onChange={(e) => setCustom((prev) => ({ ...prev, [current]: e.target.value }))}
              />
            )}
          </Flex>

          <Flex justify="flex-end" mt={3}>
            <Button size="xs" colorPalette="green" variant="solid" onClick={submit}>
              Submit
            </Button>
          </Flex>
        </Box>
      </motion.div>
    </AnimatePresence>
  );
}
