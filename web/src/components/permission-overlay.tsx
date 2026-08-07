"use client";

// The overlay above the composer when a call needs approval, mirroring the question prompt.

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import { LuShieldAlert } from "react-icons/lu";
import type { ToolPermission } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";
import { MonoList } from "./ui/display";
import { ToolLocationBadge } from "./tool-call";

import { Pre } from "./ui/semantic";

// The only runtime decisions: deny, or allow this one call. Policy is edited where policy lives.
type RuntimeDecision = "deny" | "allow_once";

interface PermissionOverlayProps {
  permission: ToolPermission;
  // A short label for what is being approved, and an optional longer detail line.
  title: string;
  detail?: string;
  // The reason's paths as a list, since joining them hard-codes a separator that belongs to a locale.
  detailPaths?: string[];
  command?: string;
  // The call's arguments, so the overlay can badge a remote location: approving should show where it runs.
  arguments?: Record<string, unknown>;
  onPermission: (requestId: string, decision: RuntimeDecision) => void;
}

export function PermissionOverlay({
  permission,
  title,
  detail,
  detailPaths,
  command,
  arguments: toolArguments,
  onPermission,
}: PermissionOverlayProps) {
  const translation = useTranslations("PermissionOverlay");
  const boxRef = useRef<HTMLDivElement>(null);

  function decide(decision: RuntimeDecision) {
    onPermission(permission.requestId, decision);
  }

  // 1 deny, 2/Enter allow once — mirrors the on-screen buttons.
  function handleKeyDown(event: React.KeyboardEvent) {
    const target = event.target instanceof HTMLElement ? event.target : null;
    const interactiveTarget = target?.closest("button,a,input,textarea,select,[role='button']");
    if (event.key === "1") {
      event.preventDefault();
      decide("deny");
    } else if (
      event.key === "2" ||
      (event.key === "Enter" && (!interactiveTarget || event.target === event.currentTarget))
    ) {
      event.preventDefault();
      decide("allow_once");
    }
  }

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  return (
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
      <Flex align="center" justify="space-between" gap={2} mb={2} flexShrink={0}>
        <Flex align="center" gap={2} minW={0}>
          <Box color="yellow.fg" flexShrink={0}>
            <LuShieldAlert size={14} />
          </Box>
          <Text textStyle="panelTitle" color="fg">
            {translation("approvalNeeded")}
          </Text>
        </Flex>
        <Flex align="center" gap={2} flexShrink={0}>
          <ToolLocationBadge arguments={toolArguments} />
        </Flex>
      </Flex>

      {/* Three things, each answering a different question: what the agent wants, exactly what will run, and why this stopped. */}
      {/* One scroll region, so the whole body scrolls together and nothing below sets a height of its own. */}
      <Flex direction="column" gap={1.5} mb={3} minH={0} overflowY="auto">
        <Text fontSize="sm" fontWeight="medium">
          {title}
        </Text>
        {command && (
          <Pre
            fontFamily="var(--app-font-mono)"
            fontSize="xs"
            color="fg.muted"
            bg="bg.subtle"
            border="1px solid"
            borderColor="border"
            borderRadius="md"
            p={2}
            m={0}
            flexShrink={0}
            // Sideways only: a long command scrolls rather than wrapping into something unlike what will run.
            overflowX="auto"
            whiteSpace="pre"
          >
            {command}
          </Pre>
        )}
        {detail && detail !== title && (
          <Box color="fg.muted" flexShrink={0}>
            <MarkdownContent content={detail} fontSize="xs" />
          </Box>
        )}
        {!!detailPaths?.length && (
          <Box flexShrink={0}>
            <MonoList items={detailPaths} />
          </Box>
        )}
      </Flex>

      <Flex align="center" justify="space-between" gap={2} flexShrink={0}>
        <Button colorPalette="red" variant="solid" onClick={() => decide("deny")}>
          {translation("deny")}
        </Button>
        <Button colorPalette="green" variant="solid" onClick={() => decide("allow_once")}>
          {translation("allowOnce")}
        </Button>
      </Flex>
    </Box>
  );
}
