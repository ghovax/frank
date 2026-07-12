"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";
import { useTranslations } from "next-intl";

export function ToolCard({ children }: { children: ReactNode }) {
  return (
    <Box borderRadius="md" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      {children}
    </Box>
  );
}

export function ToolCardHeader({
  icon,
  title,
  badges,
  open,
  collapsible = false,
  shimmer = false,
  headerBg,
  onToggle,
}: {
  icon?: ReactNode;
  title: ReactNode;
  badges?: ReactNode;
  open?: boolean;
  collapsible?: boolean;
  shimmer?: boolean;
  headerBg?: string;
  onToggle?: () => void;
}) {
  return (
    <Flex
      align="center"
      gap={1.5}
      px={2.5}
      // Fixed height (36px) — not minH: badges are taller than the title text, so a minimum
      // would let a badged row grow past a bare one. A fixed height centers whatever is inside
      // and keeps every row identical. No py — the fixed height + align="center" own it. Keep in
      // sync with the ToolGroup heading in tool-group.tsx.
      h={9}
      bg={headerBg}
      cursor={collapsible ? "pointer" : undefined}
      onClick={collapsible ? onToggle : undefined}
      userSelect="none"
    >
      {icon && (
        <Box fontSize="sm" flexShrink={0}>
          {icon}
        </Box>
      )}
      <Box
        flex={1}
        minW={0}
        overflow="hidden"
        whiteSpace="nowrap"
        textOverflow="ellipsis"
        fontSize="xs"
        fontWeight="medium"
        className={shimmer ? "running-title-shimmer" : undefined}
      >
        {title}
      </Box>
      {badges && (
        <Flex align="center" gap={1.5} flexShrink={0} minW={0}>
          {badges}
        </Flex>
      )}
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
      px={2.5}
      py={2}
      borderTop="1px solid"
      borderColor="border"
      // Recess the expanded body below the card shell (bg.subtle) so its content
      // — especially nested tool cards in the agents view — reads as raised
      // against it and gains contrast. Horizontal padding matches the header so the
      // body content lines up under the title.
      bg="bg"
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
      px={2.5}
      py={1.5}
      borderTop={borderTop ? "1px solid" : "none"}
      borderColor="border"
    >
      {children}
    </Box>
  );
}

export function ToolStatusBadge({ status }: { status: "running" | "completed" | "done" | "failed" | "input_required" }) {
  const t = useTranslations("ToolCard");
  if (status === "input_required") {
    return (
      <Badge size="sm" variant="subtle" colorPalette="yellow" borderRadius="sm" flexShrink={0}>
        {t("inputRequired")}
      </Badge>
    );
  }
  if (status === "failed") {
    return (
      <Badge size="sm" variant="subtle" colorPalette="red" borderRadius="sm" flexShrink={0}>
        {t("failed")}
      </Badge>
    );
  }
  // Completed calls carry no badge — the card's settled state speaks for itself.
  const done = status === "completed" || status === "done";
  if (done) return null;
  return (
    <Badge size="sm" variant="subtle" colorPalette="blue" borderRadius="sm" flexShrink={0}>
      {t("running")}
    </Badge>
  );
}

// Always-visible safety markers for the tool-call title bar. A tool that can
// modify state (read_only === false) shows a write badge, and a medium/high risk
// call shows its risk level. Read-only / low-risk calls stay bare.
export function ToolRiskBadges({ arguments: toolArguments }: { arguments?: Record<string, unknown> }) {
  const t = useTranslations("ToolCard");
  if (!toolArguments) return null;
  const readOnly = toolArguments.read_only !== false;
  const risk = typeof toolArguments.risk === "string" ? toolArguments.risk : "";
  const badges: ReactNode[] = [];
  if (!readOnly) {
    badges.push(
      <Badge key="write" size="sm" variant="subtle" colorPalette="orange" borderRadius="sm" flexShrink={0}>
        {t("write")}
      </Badge>
    );
  }
  if (risk === "medium" || risk === "high") {
    badges.push(
      <Badge key="risk" size="sm" variant="subtle" colorPalette={risk === "high" ? "red" : "yellow"} borderRadius="sm" flexShrink={0}>
        {risk === "high" ? t("highRisk") : t("mediumRisk")}
      </Badge>
    );
  }
  if (badges.length === 0) return null;
  return <>{badges}</>;
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
      <Text textStyle="fieldLabel" color="fg.subtle" minW="70px" flexShrink={0}>
        {label}
      </Text>
      <Box fontSize="xs" color="fg.muted" flex={1} minW={0}>
        {children}
      </Box>
    </Flex>
  );
}
