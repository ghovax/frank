"use client";

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { LuTarget } from "react-icons/lu";
import { Tooltip } from "./ui/tooltip";
import { ProseList } from "./ui/display";
import type { SessionGoal } from "@/lib/api";

// What the session is working toward, above the composer because it is a state rather than an event.
export function GoalBar({ goal, onClear }: { goal: SessionGoal; onClear: () => void }) {
  const translation = useTranslations("GoalBar");
  const text = (goal.text ?? "").trim();
  if (!text) return null;

  const status = goal.status || "active";
  const tone = status === "blocked" ? "red.fg" : status === "parked" ? "orange.fg" : "fg.muted";
  const statusLabel =
    status === "blocked" ? translation("blocked")
      : status === "parked" ? translation("waiting")
        : translation("working");

  // The requirements are the goal's substance, but taller than the bar, so they live in the hover card.
  const detail = (
    <Box whiteSpace="normal" maxW="360px">
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuTarget size={12} />
        <Text fontWeight="semibold">{statusLabel}</Text>
      </Flex>
      <Text mb={goal.requirements?.length || goal.blocker ? 2 : 0}>{text}</Text>
      {!!goal.requirements?.length && (
        <Box>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>{translation("requirements")}</Text>
          <ProseList items={goal.requirements} />
        </Box>
      )}
      {!!goal.blocker && (
        <Box mt={2}>
          <Text textStyle="fieldLabel" color="fg.subtle" mb={0.5}>{translation("blocker")}</Text>
          <Text>{goal.blocker}</Text>
        </Box>
      )}
    </Box>
  );

  return (
    <Flex align="center" gap={2} mb={2} px={2} py={1.5} borderRadius="md" border="1px solid" borderColor="border" bg="bg.subtle">
      <Tooltip content={detail} rich openDelay={200} closeDelay={60} positioning={{ placement: "top" }}>
        <Flex align="center" gap={2} flex={1} minW={0} color={tone}>
          <LuTarget size={12} />
          <Text textStyle="fieldLabel" flexShrink={0}>{statusLabel}</Text>
          <Text fontSize="sm" color="fg.muted" truncate>{text}</Text>
        </Flex>
      </Tooltip>
      <Button size="2xs" variant="outline" flexShrink={0} onClick={onClear}>
        {translation("stop")}
      </Button>
    </Flex>
  );
}
