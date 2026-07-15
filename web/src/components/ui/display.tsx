"use client";

import { Box, Flex, Span, Text, type SpanProps } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { Pre } from "./semantic";

// Structured-display building blocks shared across the app (tool views, panels,
// dialogs): a label/value field system, monospace spans/blocks, an empty hint, and a
// bordered grouping card. One consistent visual language so every fielded surface
// lines up and new ones only declare what to show.

// The fixed width of an inline field's label column, so every label + value row lines
// up. One source of truth (InlineField) rather than a literal repeated per component.
export const FIELD_LABEL_MINIMUM_W = "70px";

export function FieldList({ children }: { children: ReactNode }) {
  return (
    <Flex direction="column" gap={2}>
      {children}
    </Flex>
  );
}

/** A label stacked above its value — for long values (commands, prompts, output). */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box>
      <Text textStyle="fieldLabel" color="fg.subtle" mb={1}>
        {label}
      </Text>
      <Box fontSize="xs">{children}</Box>
    </Box>
  );
}

/** A label and value on one baseline-aligned row — for short scalar values. */
export function InlineField({ label, children, mt }: { label: string; children: ReactNode; mt?: number }) {
  return (
    <Flex align="baseline" gap={2} mt={mt}>
      <Text textStyle="fieldLabel" color="fg.subtle" minW={FIELD_LABEL_MINIMUM_W} flexShrink={0}>
        {label}
      </Text>
      <Box fontSize="xs" flex={1} minW={0}>
        {children}
      </Box>
    </Flex>
  );
}

// Monospace inline span for identifiers/paths/patterns/URLs — the scalar values that
// should read as code rather than prose. Extra Text props pass through for tuning.
export function Mono({ children, ...rest }: { children: ReactNode } & SpanProps) {
  return (
    <Span fontFamily="var(--app-font-mono)" fontSize="xs" wordBreak="break-all" {...rest}>
      {children}
    </Span>
  );
}

export function MonoBlock({ children, maxH = 64 }: { children: ReactNode; maxH?: number | string }) {
  return (
    <Pre
      m={0}
      fontFamily="var(--app-font-mono)"
      fontSize="xs"
      lineHeight="1.5"
      bg="bg.subtle"
      border="1px solid"
      borderColor="border"
      borderRadius="md"
      px={2}
      py={1.5}
      maxW="100%"
      maxH={maxH}
      overflowX="auto"
      overflowY="auto"
      whiteSpace="pre"
    >
      {children}
    </Pre>
  );
}

export function EmptyHint({ children }: { children: ReactNode }) {
  return (
    <Text fontSize="xs" color="fg.subtle" fontStyle="italic">
      {children}
    </Text>
  );
}

/** A bordered card used to group repeated items (steps, search results). */
export function Card({ children }: { children: ReactNode }) {
  return (
    <Box border="1px solid" borderColor="border" borderRadius="md" bg="bg" px={2} py={1.5}>
      {children}
    </Box>
  );
}
