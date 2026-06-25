"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { useState, type ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";

// Reusable structured-display building blocks. Tool-specific views (see
// ./registry) compose these so every tool renders with one consistent visual
// language, and new tools only need to declare which fields to show.

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
      <Text
        fontSize="xs"
        fontWeight="medium"
        color="fg.subtle"
        mb={1}
      >
        {label}
      </Text>
      <Box fontSize="xs">{children}</Box>
    </Box>
  );
}

/** A label and value on one baseline-aligned row — for short scalar values. */
export function InlineField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Flex align="baseline" gap={2}>
      <Text
        fontSize="xs"
        fontWeight="medium"
        color="fg.subtle"
        minW="70px"
        flexShrink={0}
      >
        {label}
      </Text>
      <Box fontSize="xs" flex={1} minW={0}>
        {children}
      </Box>
    </Flex>
  );
}

export function MonoBlock({ children, maxH = "260px" }: { children: ReactNode; maxH?: string }) {
  return (
    <Box
      as="pre"
      m={0}
      fontFamily="mono"
      fontSize="11px"
      lineHeight="1.5"
      bg="bg.muted"
      border="1px solid"
      borderColor="border"
      borderRadius="sm"
      px={2}
      py={1.5}
      maxH={maxH}
      overflowX="auto"
      overflowY="auto"
      whiteSpace="pre-wrap"
      wordBreak="break-word"
    >
      {children}
    </Box>
  );
}

export function Pill({
  children,
  colorPalette = "gray",
}: {
  children: ReactNode;
  colorPalette?: string;
}) {
  return (
    <Badge size="sm" variant="subtle" colorPalette={colorPalette} borderRadius="sm">
      {children}
    </Badge>
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
    <Box border="1px solid" borderColor="border" borderRadius="sm" bg="bg" px={2} py={1.5}>
      {children}
    </Box>
  );
}

export function Collapsible({
  title,
  count,
  defaultOpen = false,
  children,
}: {
  title: string;
  count?: number;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Box>
      <Flex
        align="center"
        gap={1}
        cursor="pointer"
        userSelect="none"
        color="fg.muted"
        onClick={() => setOpen((current) => !current)}
      >
        {open ? <LuChevronDown size={11} /> : <LuChevronRight size={11} />}
        <Text fontSize="11px" fontWeight="medium">
          {title}
        </Text>
        {count != null && (
          <Text fontSize="10px" color="fg.subtle">
            ({count})
          </Text>
        )}
      </Flex>
      {open && <Box mt={1.5}>{children}</Box>}
    </Box>
  );
}

// --- value coercion helpers (tool payloads arrive as `unknown`) ---

export function asString(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  return String(value);
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
