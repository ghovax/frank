"use client";

import { Badge, Box, Flex } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";
import { useTranslations } from "next-intl";
import { InlineField } from "./tool-views/primitives";

// Two looks share these primitives:
//   • "card" — a bordered, filled box (the agents panel's agent-task cards).
//   • "row"  — the borderless transcript tool-call line: a leading icon, an sm label, a
//     right-edge caret, and a body that hangs off a hairline left rule. Used by the
//     capability browser so "Skills/Tools available" read exactly like a transcript tool call.
type ToolCardVariant = "card" | "row";

export function ToolCard({ children, variant = "card" }: { children: ReactNode; variant?: ToolCardVariant }) {
  if (variant === "row") return <Box minW={0}>{children}</Box>;
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
  onToggle,
  variant = "card",
}: {
  icon?: ReactNode;
  title: ReactNode;
  badges?: ReactNode;
  open?: boolean;
  collapsible?: boolean;
  onToggle?: () => void;
  variant?: ToolCardVariant;
}) {
  if (variant === "row") {
    // Mirrors the transcript ToolCall line exactly: fit-content width, the settled/hover
    // color rule, an sm label, and the disclosure caret at the trailing edge.
    return (
      <Flex
        as={collapsible ? "button" : "div"}
        align="center"
        gap={1.5}
        h={6}
        w="fit-content"
        maxW="100%"
        minW={0}
        textAlign="left"
        cursor={collapsible ? "pointer" : undefined}
        onClick={collapsible ? onToggle : undefined}
        userSelect="none"
        color={open ? "fg" : "fg.muted"}
        _hover={collapsible ? { color: "fg" } : undefined}
      >
        {icon && (
          <Box display="flex" alignItems="center" flexShrink={0}>
            {icon}
          </Box>
        )}
        <Box minW={0} overflow="hidden" whiteSpace="nowrap" textOverflow="ellipsis" textStyle="fieldLabel" fontSize="sm" fontWeight="normal">
          {title}
        </Box>
        {badges && (
          <Flex align="center" gap={1.5} flexShrink={0} minW={0}>
            {badges}
          </Flex>
        )}
        {collapsible && (
          <Box display="flex" alignItems="center" flexShrink={0} opacity={0.7}>
            {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        )}
      </Flex>
    );
  }
  return (
    <Flex
      align="center"
      gap={1.5}
      px={2.5}
      // Fixed height — not minH: badges are taller than the title text, so a minimum
      // would let a badged row grow past a bare one. A fixed height centers whatever is inside
      // and keeps every row identical. No py — the fixed height + align="center" own it.
      h={7}
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
        textStyle="fieldLabel"
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
  variant = "card",
}: {
  children: ReactNode;
  maxH?: string;
  variant?: ToolCardVariant;
}) {
  if (variant === "row") {
    // The transcript body: content hangs off a hairline left rule, indented under the icon.
    return (
      <Box
        className="reveal-enter"
        ml="5px"
        mt={0.5}
        mb={1}
        pl={3}
        borderLeft="2px solid"
        borderColor="border.muted"
        maxH={maxH}
        overflowY={maxH ? "auto" : undefined}
        overflowX={maxH ? "auto" : undefined}
      >
        {children}
      </Box>
    );
  }
  return (
    <Box
      px={2.5}
      py={1.5}
      borderTop="1px solid"
      borderColor="border"
      // Recess the expanded body below the card shell (bg.subtle) so its content
      // reads as raised against it and gains contrast. Horizontal padding matches
      // the header so the body content lines up under the title.
      bg="bg"
      maxH={maxH}
      overflowY={maxH ? "auto" : undefined}
      overflowX={maxH ? "auto" : undefined}
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

// A label + value on one baseline row. Thin alias over the shared InlineField so
// tool-card meta and tool-view fields render identically (one component, one label
// column width).
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
    <InlineField label={label} mt={mt}>
      {children}
    </InlineField>
  );
}
