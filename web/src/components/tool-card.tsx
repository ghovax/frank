"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";

export function ToolCard({ children }: { children: ReactNode }) {
  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      {children}
    </Box>
  );
}

export function ToolCardHeader({
  icon,
  sequenceNumber,
  title,
  badges,
  open,
  collapsible = false,
  onToggle,
}: {
  icon?: ReactNode;
  sequenceNumber?: number;
  title: ReactNode;
  badges?: ReactNode;
  open?: boolean;
  collapsible?: boolean;
  onToggle?: () => void;
}) {
  return (
    <Flex
      align="center"
      gap={1.5}
      px={2}
      py={1.5}
      minH="8"
      cursor={collapsible ? "pointer" : undefined}
      onClick={collapsible ? onToggle : undefined}
      userSelect="none"
    >
      {sequenceNumber != null && (
        <Text fontSize="xs" color="fg.subtle" fontWeight="medium" flexShrink={0}>
          #{sequenceNumber}
        </Text>
      )}
      {icon && (
        <Box fontSize="sm" flexShrink={0}>
          {icon}
        </Box>
      )}
      <Text fontSize="xs" fontWeight="medium" truncate flex={1}>
        {title}
      </Text>
      {badges}
      {collapsible && (
        <Box color="fg.muted" fontSize="xs" ml="auto" flexShrink={0}>
          {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
        </Box>
      )}
    </Flex>
  );
}

export function ToolCardBody({
  children,
  maxH,
}: {
  children: ReactNode;
  maxH?: string;
}) {
  return (
    <Box
      px={2}
      py={2}
      borderTop="1px solid"
      borderColor="border"
      maxH={maxH}
      overflowY={maxH ? "auto" : undefined}
      overflowX={maxH ? "auto" : undefined}
    >
      {children}
    </Box>
  );
}

export function ToolCardSection({
  children,
  borderTop = false,
}: {
  children: ReactNode;
  borderTop?: boolean;
}) {
  return (
    <Box
      px={2}
      py={1.5}
      borderTop={borderTop ? "1px solid" : "none"}
      borderColor="border"
    >
      {children}
    </Box>
  );
}

export function ToolStatusBadge({ status }: { status: "running" | "completed" | "done" }) {
  const done = status === "completed" || status === "done";
  return (
    <Badge size="sm" variant="subtle" colorPalette={done ? "green" : "blue"} borderRadius="sm" flexShrink={0}>
      {done ? "Done" : "Running"}
    </Badge>
  );
}

export function ToolMetaRow({
  label,
  children,
  mt,
}: {
  label: string;
  children: ReactNode;
  mt?: number;
}) {
  return (
    <Flex align="baseline" gap={2} mt={mt}>
      <Text fontSize="xs" color="fg.subtle" fontWeight="medium" minW="70px" flexShrink={0}>
        {label}
      </Text>
      <Box fontSize="xs" color="fg.muted" flex={1} minW={0}>
        {children}
      </Box>
    </Flex>
  );
}
