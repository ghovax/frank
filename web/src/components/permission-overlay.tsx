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
import { useEffect, useRef } from "react";
import { LuShieldAlert } from "react-icons/lu";
import type { PermissionDecision, ToolPermission } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";

interface PermissionOverlayProps {
  permission: ToolPermission;
  // A short label for what is being approved (the tool's own display label, e.g.
  // the command or the justification) plus an optional longer detail line.
  title: string;
  detail?: string;
  onPermission: (requestId: string, decision: PermissionDecision) => void;
}

const RISK_PALETTE: Record<string, string> = { high: "red", medium: "orange", low: "gray" };
const RISK_LABEL: Record<string, string> = { high: "High", medium: "Medium", low: "Low" };

export function PermissionOverlay({ permission, title, detail, onPermission }: PermissionOverlayProps) {
  const boxRef = useRef<HTMLDivElement>(null);

  function decide(decision: PermissionDecision) {
    onPermission(permission.requestId, decision);
  }

  // 1 deny, 2 allow always, 3/Enter allow once — mirrors the on-screen buttons.
  function handleKeyDown(event: React.KeyboardEvent) {
    if (event.key === "1") {
      event.preventDefault();
      decide("deny");
    } else if (event.key === "2") {
      event.preventDefault();
      decide("allow_always");
    } else if (event.key === "3" || (event.key === "Enter" && event.target === event.currentTarget)) {
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
          mx={2}
          mb={2}
          p={3}
          borderRadius="md"
          border="1px solid"
          borderColor="border.emphasized"
          bg="bg"
          boxShadow="lg"
          maxH="50vh"
          overflowY="auto"
          _focus={{ outline: "none", boxShadow: "lg" }}
        >
          <Flex align="center" justify="space-between" gap={2} mb={2}>
            <Flex align="center" gap={2} minW={0}>
              <Box color="yellow.fg" flexShrink={0}>
                <LuShieldAlert size={14} />
              </Box>
              <Text fontSize="sm" fontWeight="bold" color="fg">
                Approval needed
              </Text>
            </Flex>
            {risk && (
              <Badge size="sm" variant="subtle" colorPalette={RISK_PALETTE[risk] ?? "gray"} borderRadius="sm" flexShrink={0}>
                {RISK_LABEL[risk] ?? risk} risk
              </Badge>
            )}
          </Flex>

          <Flex direction="column" gap={1.5} mb={3}>
            <Text fontSize="sm" fontWeight="medium">{title}</Text>
            {detail && (
              <Box color="fg.muted">
                <MarkdownContent content={detail} fontSize="xs" />
              </Box>
            )}
          </Flex>

          <Flex align="center" justify="space-between" gap={2}>
            <Button size="xs" colorPalette="red" variant="solid" onClick={() => decide("deny")}>
              Deny (1)
            </Button>
            <HStack gap={2}>
              <Button size="xs" colorPalette="blue" variant="subtle" onClick={() => decide("allow_always")}>
                Always allow (2)
              </Button>
              <Button size="xs" colorPalette="green" variant="solid" onClick={() => decide("allow_once")}>
                Allow once (3/Enter)
              </Button>
            </HStack>
          </Flex>
        </Box>
      </motion.div>
    </AnimatePresence>
  );
}
