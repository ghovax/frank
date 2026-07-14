"use client";

import { Box, HStack, Stack, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import type { ChatGPTUsage } from "@/lib/api";

type Translator = ReturnType<typeof useTranslations<"ChatGPTAuthControl">>;

// The 5h/weekly split is not pinned to a fixed slot across accounts, so label a
// window by its own length rather than trusting a "primary"/"secondary" position.
function windowLabel(t: Translator, minutes: number): string {
  if (minutes === 10080) return t("usageWeekly");
  if (minutes % 1440 === 0) return t("usageDaysShort", { count: minutes / 1440 });
  if (minutes % 60 === 0) return t("usageHoursShort", { count: minutes / 60 });
  return t("usageMinutesShort", { count: minutes });
}

function resetsInLabel(t: Translator, resetsAt: number | null): string | null {
  if (!resetsAt) return null;
  const seconds = resetsAt - Math.floor(Date.now() / 1000);
  if (seconds <= 0) return null;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const parts: string[] = [];
  if (days) parts.push(t("usageDaysShort", { count: days }));
  if (hours) parts.push(t("usageHoursShort", { count: hours }));
  if (!days && !hours) parts.push(t("usageMinutesShort", { count: Math.max(minutes, 1) }));
  return t("usageResetsIn", { duration: parts.join(" ") });
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
  const t = useTranslations("ChatGPTAuthControl");
  const windows = usage?.windows ?? [];
  return (
    <Box>
      <Text textStyle="fieldLabel" mb={1.5}>
        {t("usageTitle")}
      </Text>
      {windows.length === 0 ? (
        <Text fontSize="xs" color="fg.muted">
          {t("usagePending")}
        </Text>
      ) : (
        <Stack gap={2.5}>
          {windows.map((window) => {
            const percent = Math.min(Math.max(window.used_percent, 0), 100);
            const resets = resetsInLabel(t, window.resets_at);
            return (
              <Box key={window.key}>
                <HStack justify="space-between" mb={1} gap={4}>
                  <Text fontSize="xs" fontWeight="medium">
                    {windowLabel(t, window.window_minutes)}
                  </Text>
                  <Text fontSize="xs" color="fg.muted">
                    {t("usageUsedPercent", { percent: Math.round(percent) })}
                    {resets ? ` · ${resets}` : ""}
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
              </Box>
            );
          })}
        </Stack>
      )}
    </Box>
  );
}
