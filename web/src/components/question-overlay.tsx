"use client";

// A prominent overlay above the composer when the agent asks a question, so it cannot be missed.

import { Box, Button, Flex, IconButton, Input, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { LuCheck, LuChevronLeft, LuChevronRight, LuSkipForward, LuX } from "react-icons/lu";
import type { QuestionAnswer, ToolQuestion } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";

interface QuestionOverlayProps {
  question: ToolQuestion;
  onQuestion: (requestId: string, answers: QuestionAnswer[]) => void;
  // Dismiss the whole prompt without answering; undefined hides the close affordance.
  onDismiss?: (requestId: string) => void;
}

export function QuestionOverlay({ question, onQuestion, onDismiss }: QuestionOverlayProps) {
  const translation = useTranslations("QuestionOverlay");
  const items = question.questions;
  const [current, setCurrent] = useState(0);
  const [selected, setSelected] = useState<Record<number, string[]>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const [skipped, setSkipped] = useState<Record<number, boolean>>({});
  const boxRef = useRef<HTMLDivElement>(null);

  const total = items.length;
  const item = items[current];

  function toggle(index: number, label: string, multiple: boolean) {
    setSkipped((previous) => (previous[index] ? { ...previous, [index]: false } : previous));
    const previouslySelected = selected[index] ?? [];
    setSelected((previous) => {
      const active = previous[index] ?? [];
      if (!multiple) {
        return { ...previous, [index]: active.length === 1 && active[0] === label ? [] : [label] };
      }
      return {
        ...previous,
        [index]: active.includes(label)
          ? active.filter((value) => value !== label)
          : [...active, label],
      };
    });
    // Auto-advance for single-choice questions, which is navigation convenience rather than auto-submit.
    if (!multiple && index === current && current < total - 1) {
      const isDeselection = previouslySelected.length === 1 && previouslySelected[0] === label;
      if (!isDeselection) {
        setCurrent((previous) => previous + 1);
      }
    }
  }

  // The answer for one question: its typed text, else the selected labels, else empty.
  function answerFor(index: number): QuestionAnswer {
    if (skipped[index]) return items[index].multiple ? [] : "";
    const text = (custom[index] ?? "").trim();
    if (text) return text;
    const chosen = selected[index] ?? [];
    return items[index].multiple ? chosen : (chosen[0] ?? "");
  }

  function submit() {
    onQuestion(
      question.requestId,
      items.map((_, index) => answerFor(index)),
    );
  }

  function skipCurrent() {
    setSkipped((previous) => ({ ...previous, [current]: true }));
    setSelected((previous) => ({ ...previous, [current]: [] }));
    setCustom((previous) => ({ ...previous, [current]: "" }));
    if (current < total - 1) {
      setCurrent((previous) => previous + 1);
    } else {
      // Last question skipped — submit with everything gathered so far.
      onQuestion(
        question.requestId,
        items.map((_, index) =>
          index === current ? (items[index].multiple ? [] : "") : answerFor(index),
        ),
      );
    }
  }

  const multiple = !!item.multiple;
  const customEnabled = item.custom !== false;
  const hasOptions = !!item.options && item.options.length > 0;
  const active = selected[current] ?? [];
  const text = custom[current] ?? "";
  const isSkipped = !!skipped[current];
  const allAnswered = items.every((_, index) => {
    if (skipped[index]) return true;
    const customText = (custom[index] ?? "").trim();
    if (customText) return true;
    const choices = selected[index] ?? [];
    return choices.length > 0;
  });

  return (
    <Box
      ref={boxRef}
      w="full"
      mb={2}
      p={3}
      borderRadius="md"
      border="1px solid"
      borderColor="border"
      bg="bg.panel"
      boxShadow="panel"
      maxH="50vh"
      overflowY="auto"
    >
      <Flex align="center" justify="space-between" gap={2}>
        <Text textStyle="panelTitle" color="fg">
          {translation("questionProgress", { current: current + 1, total })}
        </Text>
        <Flex align="center" gap={1}>
          {onDismiss && (
            <IconButton
              aria-label={translation("dismissLabel")}
              title={translation("dismissTitle")}
              variant="ghost"
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
          <Flex direction="column" gap={1.5}>
            {item.options!.map((option) => {
              const isSelected = !text && !isSkipped && active.includes(option.label);
              return (
                <Button
                  key={option.label}
                  variant="outline"
                  w="full"
                  h="auto"
                  gap={2.5}
                  px={2.5}
                  py={1.5}
                  justifyContent="flex-start"
                  borderColor={isSelected ? "blue.solid" : "border"}
                  bg={isSelected ? "blue.subtle" : "bg"}
                  textAlign="left"
                  whiteSpace="normal"
                  transition="all 120ms"
                  _hover={{ borderColor: isSelected ? "blue.solid" : "border.emphasized" }}
                  onClick={() => {
                    if (!text) toggle(current, option.label, multiple);
                  }}
                >
                  <Box
                    w={4}
                    h={4}
                    flexShrink={0}
                    borderRadius={multiple ? "sm" : "full"}
                    border="1px solid"
                    borderColor={isSelected ? "blue.solid" : "border"}
                    bg={isSelected ? "blue.solid" : "transparent"}
                    display="flex"
                    alignItems="center"
                    justifyContent="center"
                  >
                    {isSelected && <LuCheck size={10} color="white" strokeWidth={3} />}
                  </Box>
                  <Flex direction="column" minW={0} flex={1}>
                    <MarkdownContent content={option.label} fontSize="sm" />
                    {option.description && (
                      <Box color="fg.muted">
                        <MarkdownContent content={option.description} fontSize="xs" />
                      </Box>
                    )}
                  </Flex>
                </Button>
              );
            })}
          </Flex>
        )}

        {customEnabled && (
          <Input
            placeholder={translation("customPlaceholder")}
            value={text}
            onChange={(event) => {
              const value = event.target.value;
              if (value)
                setSkipped((previous) =>
                  previous[current] ? { ...previous, [current]: false } : previous,
                );
              setCustom((previous) => ({ ...previous, [current]: value }));
            }}
          />
        )}

        {isSkipped && (
          <Text fontSize="xs" color="fg.subtle">
            {translation("skippedNote")}
          </Text>
        )}
      </Flex>

      <Flex justify="space-between" align="center" mt={3} gap={2}>
        <Flex align="center" gap={1}>
          {total > 1 && (
            <Button
              variant="subtle"
              colorPalette="gray"
              disabled={current === 0}
              onClick={() => setCurrent((previous) => previous - 1)}
            >
              <LuChevronLeft size={12} />
              {translation("back")}
            </Button>
          )}
          {total > 1 && current < total - 1 && (
            <Button
              variant="subtle"
              colorPalette="gray"
              onClick={() => setCurrent((previous) => previous + 1)}
            >
              <LuChevronRight size={12} />
              {translation("next")}
            </Button>
          )}
          <Button variant="subtle" colorPalette="red" onClick={skipCurrent}>
            <LuSkipForward size={12} />
            {translation("skip")}
          </Button>
        </Flex>
        <Button colorPalette="green" variant="solid" onClick={submit} disabled={!allAnswered}>
          {translation("submit")}
        </Button>
      </Flex>
    </Box>
  );
}
