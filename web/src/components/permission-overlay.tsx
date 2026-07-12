"use client";

// A prominent overlay that appears above the chat input when a tool call needs
// the user's approval (a permission request). Mirrors QuestionOverlay so the two
// input-required prompts read and behave identically — the user cannot miss it,
// it takes focus for keyboard shortcuts, and it blocks the composer until the
// decision is made. Moved out of the tool card (where it used to render inline)
// so a pending approval always grabs attention at the bottom of the chat, even
// when the triggering card is scrolled out of view.

import { Badge, Box, Button, Flex, HStack, Text } from "@chakra-ui/react";
import { AnimatePresence, motion } from "motion/react";
import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import { LuShieldAlert } from "react-icons/lu";
import type { PermissionDecision, ToolPermission } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";
import { ToolLocationBadge } from "./tool-call";

interface PermissionOverlayProps {
  permission: ToolPermission;
  // A short label for what is being approved (the tool's own display label, e.g.
  // the command or the justification) plus an optional longer detail line.
  title: string;
  detail?: string;
  command?: string;
  // The tool call's arguments, so the overlay can badge a remote `location` — a user
  // approving an operation should see *where* it runs, not just its risk.
  arguments?: Record<string, unknown>;
  onPermission: (requestId: string, decision: PermissionDecision) => void;
}

const RISK_PALETTE: Record<string, string> = { high: "red", medium: "orange", low: "gray" };
const RISK_KEY: Record<string, string> = { high: "riskHigh", medium: "riskMedium", low: "riskLow" };

export function PermissionOverlay({ permission, title, detail, command, arguments: toolArguments, onPermission }: PermissionOverlayProps) {
  const t = useTranslations("PermissionOverlay");
  const boxRef = useRef<HTMLDivElement>(null);

  function decide(decision: PermissionDecision) {
    onPermission(permission.requestId, decision);
  }

  // 1 deny, 2 allow always, 3/Enter allow once — mirrors the on-screen buttons.
  function handleKeyDown(event: React.KeyboardEvent) {
    const target = event.target instanceof HTMLElement ? event.target : null;
    const interactiveTarget = target?.closest("button,a,input,textarea,select,[role='button']");
    if (event.key === "1") {
      event.preventDefault();
      decide("deny");
    } else if (event.key === "2") {
      event.preventDefault();
      decide("allow_always");
    } else if (event.key === "3" || (event.key === "Enter" && (!interactiveTarget || event.target === event.currentTarget))) {
      event.preventDefault();
      decide("allow_once");
    }
  }

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  const risk = permission.risk ? String(permission.risk).toLowerCase() : "";

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
          tabIndex={0}
          onKeyDown={handleKeyDown}
          w="full"
          mb={2}
          p={3}
          borderRadius="md"
          border="1px solid"
          borderColor="border"
          bg="bg.panel"
          boxShadow="panel"
          maxH="50vh"
          overflow="hidden"
          display="flex"
          flexDirection="column"
          _focus={{ outline: "none" }}
        >
          <Flex align="center" justify="space-between" gap={2} mb={2}>
            <Flex align="center" gap={2} minW={0}>
              <Box color="yellow.fg" flexShrink={0}>
                <LuShieldAlert size={14} />
              </Box>
              <Text fontSize="sm" fontWeight="bold" color="fg">
                {t("approvalNeeded")}
              </Text>
            </Flex>
            <Flex align="center" gap={2} flexShrink={0}>
              <ToolLocationBadge arguments={toolArguments} />
              {risk && (
                <Badge size="sm" variant="subtle" colorPalette={RISK_PALETTE[risk] ?? "gray"} borderRadius="sm" flexShrink={0}>
                  {t("riskBadge", { level: RISK_KEY[risk] ? t(RISK_KEY[risk] as Parameters<typeof t>[0]) : risk })}
                </Badge>
              )}
            </Flex>
          </Flex>

          <Flex direction="column" gap={1.5} mb={3} minH={0}>
            <Text fontSize="sm" fontWeight="medium">{title}</Text>
            {command ? (
              <Box
                as="pre"
                fontFamily="var(--app-font-mono)"
                fontSize="xs"
                color="fg.muted"
                bg="bg.subtle"
                border="1px solid"
                borderColor="border"
                borderRadius="md"
                p={2}
                m={0}
                maxH="min(28vh, 260px)"
                minH={0}
                overflow="auto"
                whiteSpace="pre"
              >
                {command}
              </Box>
            ) : detail ? (
              <Box color="fg.muted" minH={0} overflow="auto">
                <MarkdownContent content={detail} fontSize="xs" />
              </Box>
            ) : null}
          </Flex>

          <Flex align="center" justify="space-between" gap={2} flexShrink={0}>
            <Button colorPalette="red" variant="solid" onClick={() => decide("deny")}>
              {t("deny")}
            </Button>
            <HStack gap={2}>
              <Button colorPalette="blue" variant="subtle" onClick={() => decide("allow_always")}>
                {t("allowAlways")}
              </Button>
              <Button colorPalette="green" variant="solid" onClick={() => decide("allow_once")}>
                {t("allowOnce")}
              </Button>
            </HStack>
          </Flex>
        </Box>
      </motion.div>
    </AnimatePresence>
  );
}
