"use client";

import { Badge, Box, Flex, Text, type TextProps } from "@chakra-ui/react";
import { useState, type ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";

// Reusable structured-display building blocks. Tool-specific views (see
// ./registry) compose these so every tool renders with one consistent visual
// language, and new tools only need to declare which fields to show.

// The fixed width of an inline field's label column, so every label + value row
// lines up across tool cards and tool views. One source of truth (InlineField,
// ToolMetaRow) rather than a literal repeated per component.
export const FIELD_LABEL_MIN_W = "70px";

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
        textStyle="fieldLabel"
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
export function InlineField({ label, children, mt }: { label: string; children: ReactNode; mt?: number }) {
  return (
    <Flex align="baseline" gap={2} mt={mt}>
      <Text
        textStyle="fieldLabel"
        color="fg.subtle"
        minW={FIELD_LABEL_MIN_W}
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

// Monospace inline span for identifiers/paths/patterns/URLs — the scalar values
// that should read as code rather than prose. Extra Text props (color, fontSize,
// truncate, whiteSpace) pass through for the few call sites that tune it.
export function Mono({ children, ...rest }: { children: ReactNode } & TextProps) {
  return (
    <Text as="span" fontFamily="var(--app-font-mono)" fontSize="xs" wordBreak="break-all" {...rest}>
      {children}
    </Text>
  );
}

export function MonoBlock({ children, maxH = 64 }: { children: ReactNode; maxH?: number | string }) {
  return (
    <Box
      as="pre"
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
    <Box border="1px solid" borderColor="border" borderRadius="md" bg="bg" px={2} py={1.5}>
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
        {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
        <Text textStyle="fieldLabel">
          {title}
        </Text>
        {count != null && (
          <Text fontSize="xs" color="fg.subtle">
            ({count})
          </Text>
        )}
      </Flex>
      {open && <Box mt={1.5}>{children}</Box>}
    </Box>
  );
}

// Value coercion helpers: tool payloads arrive as `unknown` and need typed accessors.

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
