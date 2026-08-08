"use client";

import { Box, Button, Flex, Span, Spinner, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { LuCircleCheck, LuCircleSlash, LuDot, LuSquare, LuTarget, LuX } from "react-icons/lu";
import { Tooltip } from "./ui/tooltip";
import { ConfirmDialog } from "./ui/confirm-dialog";
import { ProseList } from "./ui/display";
import type { SessionGoal } from "@/lib/api";

// What the session is working toward, above the composer because it is a state rather than an event.
export function GoalBar({ goal, onClear }: { goal: SessionGoal; onClear: () => void }) {
  const translation = useTranslations("GoalBar");
  const [confirming, setConfirming] = useState(false);
  const text = (goal.text ?? "").trim();
  if (!text) return null;

  const status = goal.status || "active";
  // Being read is not a status the goal holds, so it colours the label without becoming one.
  const reviewing = !!goal.reviewing && status === "active";
  // A resolved goal stops driving the session but stays on the record, so the bar keeps showing it.
  const resolved = status === "satisfied" || status === "cleared";
  // The status is the one thing here that changes, so it is the one thing that carries a colour.
  const tone =
    status === "blocked"
      ? "red.fg"
      : status === "parked"
        ? "orange.fg"
        : status === "satisfied"
          ? "green.fg"
          : status === "cleared"
            ? "fg.muted"
            : "blue.fg";
  const statusLabel = reviewing
    ? translation("reviewing")
    : status === "blocked"
      ? translation("blocked")
      : status === "parked"
        ? translation("waiting")
        : status === "satisfied"
          ? translation("satisfied")
          : status === "cleared"
            ? translation("cleared")
            : translation("working");

  // The requirements are the goal's substance, but taller than the bar, so they live in the hover card.
  const detail = (
    <Box whiteSpace="normal" maxW="360px">
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuTarget size={12} />
        <Text fontWeight="semibold">{statusLabel}</Text>
      </Flex>
      <Text mb={goal.requirements?.length || goal.blocker || goal.evidence ? 2 : 0}>{text}</Text>
      {!!goal.requirements?.length && (
        <Box>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>
            {translation("requirements")}
          </Text>
          <ProseList items={goal.requirements} />
        </Box>
      )}
      {/* What the review last decided: why it is stuck, what convinced it, or what it asked for next. */}
      {!!goal.blocker && (
        <Box mt={2}>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>
            {translation("blocker")}
          </Text>
          <Text>{goal.blocker}</Text>
        </Box>
      )}
      {!!goal.evidence && (
        <Box mt={2}>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>
            {translation("evidence")}
          </Text>
          <Text>{goal.evidence}</Text>
        </Box>
      )}
      {status === "active" && !!goal.direction && (
        <Box mt={2}>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>
            {translation("direction")}
          </Text>
          <Text>{goal.direction}</Text>
        </Box>
      )}
    </Box>
  );

  return (
    <>
      <Flex
        align="center"
        gap={2}
        mb={3}
        px={3}
        py={2}
        borderRadius="md"
        borderWidth="1px"
        borderColor="border.muted"
        boxShadow="panel"
        bg="bg.panel"
      >
        <Tooltip
          content={detail}
          rich
          openDelay={200}
          closeDelay={60}
          positioning={{ placement: "top" }}
        >
          <Flex align="center" gap={2} flex={1} minW={0} color={tone}>
            {/* Work in progress is shown as motion; a stopped goal is shown as the thing that stopped it. */}
            <Box
              display="flex"
              alignItems="center"
              justifyContent="center"
              flexShrink={0}
              boxSize="13px"
            >
              {status === "active" ? (
                <Spinner boxSize="13px" borderWidth="1.5px" color={tone} />
              ) : status === "satisfied" ? (
                <LuCircleCheck size={13} />
              ) : (
                <LuCircleSlash size={13} />
              )}
            </Box>
            {/* One size, so the two read as one line, but told apart three ways: the label carries the
                status colour and the heavier weight, the goal carries the plain foreground, and a dot
                sits between them — the same separator the record's qualifiers use. */}
            <Text
              textStyle="xs"
              fontWeight="medium"
              flexShrink={0}
              display={{ base: "none", sm: "block" }}
            >
              {statusLabel}
            </Text>
            <Box
              display={{ base: "none", sm: "flex" }}
              alignItems="center"
              flexShrink={0}
              // The glyph's box is mostly empty around a small dot, so the row's gap is pulled back in.
              mx="-9px"
            >
              <LuDot size={20} style={{ opacity: 0.7 }} />
            </Box>
            <Text textStyle="xs" color="fg" truncate minW={0}>
              {text}
            </Text>
          </Flex>
        </Tooltip>
        {/* Named as well as drawn: the one control here ends the thing the bar is about, so it says which. */}
        <Button
          title={translation(resolved ? "dismiss" : "stop")}
          size="2xs"
          variant="plain"
          px={1}
          gap={1}
          colorPalette={resolved ? "gray" : "red"}
          color={resolved ? "fg.subtle" : "red.fg"}
          flexShrink={0}
          onClick={() => (resolved ? onClear() : setConfirming(true))}
        >
          {resolved ? <LuX size={13} /> : <LuSquare size={13} />}
          {/* The goal itself yields the width first; on the narrowest bars the icon stands on its own. */}
          <Span textStyle="xs" display={{ base: "none", sm: "inline" }}>
            {translation(resolved ? "dismiss" : "stop")}
          </Span>
        </Button>
      </Flex>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={translation("stopTitle")}
        confirmLabel={translation("stopConfirm")}
        danger
        onConfirm={onClear}
      >
        {translation("stopBody")}
      </ConfirmDialog>
    </>
  );
}
