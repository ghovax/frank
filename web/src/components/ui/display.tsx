"use client";

import { Box, Flex, List, Span, Text, type SpanProps } from "@chakra-ui/react";
import { createContext, useContext, type ReactNode } from "react";
import { Pre } from "./semantic";

// The structured-display building blocks shared across the app.

// The fixed width of an inline field's label column, stated once so every row lines up.
export const FIELD_LABEL_MINIMUM_W = "70px";

export function FieldList({ children }: { children: ReactNode }) {
  return (
    // A list with nothing in it takes no room, which matters because a claimed field renders nothing.
    <Flex direction="column" gap={2} css={{ "&:empty": { display: "none" } }}>
      {children}
    </Flex>
  );
}

// One tool row, one scope: what the call already showed, the result does not repeat.
const ShownFields = createContext<Set<string> | null>(null);

export function FieldScope({ children }: { children: ReactNode }) {
  // A fresh set per render pass, so a re-render starts from empty and every field is claimed again.
  const shown = new Set<string>();
  return <ShownFields.Provider value={shown}>{children}</ShownFields.Provider>;
}

/** Whether this label is the first of its name in this row. Outside a scope, everything shows. */
function claimField(label: string, shown: Set<string> | null): boolean {
  if (!shown) return true;
  if (shown.has(label)) return false;
  shown.add(label);
  return true;
}

/** A label stacked above its value — for long values (commands, prompts, output). */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  const shown = useContext(ShownFields);
  if (!claimField(label, shown)) return null;
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

export function InlineField({
  label,
  children,
  mt,
}: {
  label: string;
  children: ReactNode;
  mt?: number;
}) {
  const shown = useContext(ShownFields);
  if (!claimField(label, shown)) return null;
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

// A monospace inline span for the scalar values that should read as code rather than prose.
export function Mono({ children, ...rest }: { children: ReactNode } & SpanProps) {
  return (
    <Span fontFamily="var(--app-font-mono)" fontSize="xs" wordBreak="break-all" {...rest}>
      {children}
    </Span>
  );
}

export function MonoBlock({
  children,
  maxH = 64,
}: {
  children: ReactNode;
  maxH?: number | string;
}) {
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

/** Several monospace values as a real bullet list, for a field that holds a set rather than one thing. */
export function MonoList({ items }: { items: string[] }) {
  return (
    <List.Root pl={4} fontSize="xs" listStyleType="disc">
      {items.map((item) => (
        <List.Item key={item} mb={0.5} _last={{ mb: 0 }}>
          <Span fontFamily="var(--app-font-mono)" wordBreak="break-all">
            {item}
          </Span>
        </List.Item>
      ))}
    </List.Root>
  );
}

/** Several sentences as a real bullet list, the prose counterpart of the monospace one. */
export function ProseList({ items }: { items: string[] }) {
  return (
    <List.Root pl={4} fontSize="xs" listStyleType="disc">
      {items.map((item, index) => (
        <List.Item key={index} mb={0.5} _last={{ mb: 0 }}>
          {item}
        </List.Item>
      ))}
    </List.Root>
  );
}

export function EmptyHint({ children }: { children: ReactNode }) {
  return (
    <Text fontSize="xs" color="fg.subtle" fontStyle="italic">
      {children}
    </Text>
  );
}

/** A bordered card grouping repeated items, opening a field scope of its own because it is the repetition. */
export function Card({ children }: { children: ReactNode }) {
  return (
    <Box border="1px solid" borderColor="border" borderRadius="md" bg="bg" px={2} py={1.5}>
      <FieldScope>{children}</FieldScope>
    </Box>
  );
}
