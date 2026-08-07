"use client";

import { Box, HStack, Stack, Text } from "@chakra-ui/react";
import { useFormatter, useNow, useTranslations } from "next-intl";
import type { ChatGPTUsage } from "@/lib/api";

type Translator = ReturnType<typeof useTranslations<"ChatGPTAuthControl">>;

// Label a window by its own length: the split is not pinned to a fixed slot across accounts.
function windowLabel(translation: Translator, minutes: number): string {
  if (minutes === 10080) return translation("usageWeekly");
  if (minutes % 1440 === 0) return translation("usageDaysShort", { count: minutes / 1440 });
  if (minutes % 60 === 0) return translation("usageHoursShort", { count: minutes / 60 });
  return translation("usageMinutesShort", { count: minutes });
}

// The plan as the provider reports it, shown unlabelled rather than hidden or renamed.
function planLabel(translation: Translator, planType: string): string {
  switch (planType) {
    case "free":
      return translation("usagePlanFree");
    case "plus":
      return translation("usagePlanPlus");
    case "pro":
      return translation("usagePlanPro");
    case "team":
      return translation("usagePlanTeam");
    case "enterprise":
      return translation("usagePlanEnterprise");
    default:
      return planType;
  }
}

function meterColor(percent: number): string {
  if (percent >= 90) return "red.500";
  if (percent >= 70) return "orange.400";
  return "green.500";
}

/** The account's rate-limit meters, captured from the last turn's headers. */
export function ChatGPTUsageMeters({ usage }: { usage: ChatGPTUsage | null }) {
  const translation = useTranslations("ChatGPTAuthControl");
  // `relativeTime` owns the wording, and `useNow` re-renders it so the countdown stays honest.
  const format = useFormatter();
  const now = useNow({ updateInterval: 60 * 1000 });
  const windows = usage?.windows ?? [];
  // Nothing is captured until the first turn after sign-in, so hide the section rather than show a placeholder.
  if (windows.length === 0) return null;
  return (
    <Box>
      <HStack justify="space-between" mb={1.5} gap={4}>
        <Text textStyle="fieldLabel">{translation("usageTitle")}</Text>
        {/* The plan is why a percentage is what it is, since a small allowance goes quickly. */}
        {usage?.plan_type ? (
          <Text fontSize="xs" color="fg.muted">
            {planLabel(translation, usage.plan_type)}
          </Text>
        ) : null}
      </HStack>
      <Stack gap={2.5}>
        {windows.map((window) => {
          const percent = Math.min(Math.max(window.used_percent, 0), 100);
          // `resets_at` is unix seconds: show the countdown only while it is still in the future.
          const resetsAt = window.resets_at ? new Date(window.resets_at * 1000) : null;
          const resets =
            resetsAt && resetsAt.getTime() > now.getTime()
              ? format.relativeTime(resetsAt, now)
              : null;
          return (
            <Box key={window.key}>
              <HStack justify="space-between" mb={1} gap={4}>
                <Text fontSize="xs" fontWeight="medium">
                  {windowLabel(translation, window.window_minutes)}
                </Text>
                <Text fontSize="xs" color="fg.muted">
                  {translation("usageUsedPercent", { percent: Math.round(percent) })}
                </Text>
              </HStack>
              <Box h="6px" bg="bg.muted" borderRadius="full" overflow="hidden">
                <Box
                  h="full"
                  w={`${percent}%`}
                  bg={meterColor(percent)}
                  borderRadius="full"
                  transition="width 0.3s ease"
                />
              </Box>
              {resets ? (
                <HStack justify="space-between" mt={1} gap={4}>
                  <Text fontSize="xs" color="fg.subtle">
                    {translation("usageResetsLabel")}
                  </Text>
                  <Text fontSize="xs" color="fg.muted">
                    {resets}
                  </Text>
                </HStack>
              ) : null}
            </Box>
          );
        })}
      </Stack>
    </Box>
  );
}
