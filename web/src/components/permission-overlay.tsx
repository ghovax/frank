"use client";

// A prominent overlay that appears above the chat input when a tool call needs
// the user's approval (a permission request). Mirrors QuestionOverlay so the two
// input-required prompts read and behave identically — the user cannot miss it,
// and it takes focus for keyboard shortcuts. Moved out of the tool card (where it
// used to render inline) so a pending approval always grabs attention at the
// bottom of the chat, even when the triggering card is scrolled out of view.
//
// It does not close the composer. The session is parked, not gone: a message typed
// while this is up is queued and sent as soon as the decision is made, so the
// prompt is the only thing waiting on the person, and their own next thought is
// not.

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useEffect, useRef } from "react";
import { LuShieldAlert } from "react-icons/lu";
import type { ToolPermission } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";
import { MonoList } from "./ui/display";
import { ToolLocationBadge } from "./tool-call";
import { RISK_LABEL_KEY, RISK_PALETTE as SHARED_RISK_PALETTE } from "@shared/status";

import { Pill } from "./ui/pill";
import { Pre } from "./ui/semantic";

// The only runtime decisions that exist: deny, or allow this one call. A standing allow
// would quietly write a rule into the session's policy from inside a prompt about one
// command; the policy is edited where policy lives — the permission mode under the composer,
// and the command rules in Settings — so the overlay never offers one.
type RuntimeDecision = "deny" | "allow_once";

interface PermissionOverlayProps {
  permission: ToolPermission;
  // A short label for what is being approved (the tool's own display label, e.g.
  // the command or the explanation) plus an optional longer detail line.
  title: string;
  detail?: string;
  // The paths the reason names, rendered as a list rather than folded into `detail`.
  // A set of paths is several values, and joining them into a sentence both hides that
  // and hard-codes a separator that belongs to a locale.
  detailPaths?: string[];
  command?: string;
  // The tool call's arguments, so the overlay can badge a remote `location` — a user
  // approving an operation should see *where* it runs, not just its risk.
  arguments?: Record<string, unknown>;
  onPermission: (requestId: string, decision: RuntimeDecision) => void;
}

// Shared, because the phone shows the same badge and had been showing a bare lowercase
// `medium` where this says "Medium risk".
const RISK_PALETTE = SHARED_RISK_PALETTE;
const RISK_KEY = RISK_LABEL_KEY;

export function PermissionOverlay({ permission, title, detail, detailPaths, command, arguments: toolArguments, onPermission }: PermissionOverlayProps) {
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
    } else if (event.key === "2" || (event.key === "Enter" && (!interactiveTarget || event.target === event.currentTarget))) {
      event.preventDefault();
      decide("allow_once");
    }
  }

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  const risk = permission.risk ? String(permission.risk).toLowerCase() : "";

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
              {risk && (
                <Pill colorPalette={RISK_PALETTE[risk] ?? "gray"}>
                  {translation("riskBadge", { level: RISK_KEY[risk] ? translation(RISK_KEY[risk] as Parameters<typeof translation>[0]) : risk })}
                </Pill>
              )}
            </Flex>
          </Flex>

          {/* Three things, and each answers a different question a person has before deciding:
              what the agent is trying to do (the title — its own explanation of the call),
              exactly what will run (the command), and what made this stop for approval (the
              detail). These used to be an either/or, so a call with a command never showed
              the reason for it and the prompt was a bare line of shell with no case for it. */}
          {/* One scroll region, and it is this one. Each part used to carry its own `overflow:
              auto`, so a card taller than its 50vh cap resolved into three independent
              scrollers stacked on top of each other — and the worst of them was the file list,
              which a person reads to decide *what* is being granted: the reason said the
              command reads files outside the working directory, and the files themselves
              arrived in a two-line window with a scrollbar in it. Nothing below sets a height
              now, so every path renders; when the whole card runs out of room the body scrolls
              as one and the header and the two buttons stay where they are. */}
          <Flex direction="column" gap={1.5} mb={3} minH={0} overflowY="auto">
            <Text fontSize="sm" fontWeight="medium">{title}</Text>
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
                // Sideways only: the command is held on one line per line deliberately, so a
                // long line scrolls across rather than wrapping into something that no longer
                // looks like what will run.
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
