"use client";

import { Box, HStack, Stack, Text } from "@chakra-ui/react";
import { useFormatter, useNow, useTranslations } from "next-intl";
import type { ChatGPTUsage } from "@/lib/api";

type Translator = ReturnType<typeof useTranslations<"ChatGPTAuthControl">>;

// The 5h/weekly split is not pinned to a fixed slot across accounts, so label a
// window by its own length rather than trusting a "primary"/"secondary" position.
function windowLabel(translation: Translator, minutes: number): string {
  if (minutes === 10080) return translation("usageWeekly");
  if (minutes % 1440 === 0) return translation("usageDaysShort", { count: minutes / 1440 });
  if (minutes % 60 === 0) return translation("usageHoursShort", { count: minutes / 60 });
  return translation("usageMinutesShort", { count: minutes });
}

function meterColor(percent: number): string {
  if (percent >= 90) return "red.500";
  if (percent >= 70) return "orange.400";
  return "green.500";
}

/**
 * The account's ChatGPT/Codex rate-limit meters (a rolling window and the weekly
 * window). Values are captured from the last turn's `x-codex-*` headers, so before
 * the first turn after sign-in there is nothing to show — hence the pending notice.
 *
 * Shared by the Settings sign-in control and the chat-input token view, so labels
 * stay in the one `ChatGPTAuthControl` namespace regardless of the host surface.
 */
export function ChatGPTUsageMeters({ usage }: { usage: ChatGPTUsage | null }) {
  const translation = useTranslations("ChatGPTAuthControl");
  // relativeTime owns the "in 3 hours" wording — locale-correct, rounded, and
  // pluralized — so the reset countdown is not hand-rolled from day/hour/minute
  // math. `useNow` supplies the reference point and re-renders it on an interval so
  // the countdown stays live while the panel is open.
  const format = useFormatter();
  const now = useNow({ updateInterval: 60 * 1000 });
  const windows = usage?.windows ?? [];
  // Nothing is captured until the first turn after sign-in, so hide the section
  // entirely rather than showing a placeholder.
  if (windows.length === 0) return null;
  return (
    <Box>
      <Text textStyle="fieldLabel" mb={1.5}>
        {translation("usageTitle")}
      </Text>
      <Stack gap={2.5}>
        {windows.map((window) => {
          const percent = Math.min(Math.max(window.used_percent, 0), 100);
          // resets_at is a unix timestamp in seconds; show the countdown only while
          // it is still in the future (a past/absent reset just hides the row).
          const resetsAt = window.resets_at ? new Date(window.resets_at * 1000) : null;
          const resets = resetsAt && resetsAt.getTime() > now.getTime()
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
