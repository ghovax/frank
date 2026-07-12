"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useFormatter, useTranslations } from "next-intl";
import { LuArrowDown, LuArrowUp, LuGitBranch } from "react-icons/lu";
import { Tooltip } from "./ui/tooltip";
import { InlineField } from "./tool-views/primitives";
import type { DirectoryStatus } from "@/lib/use-directory-status";

// The workspace's Git status: branch, working-tree dirtiness and ahead/behind for the
// active working directory. Renders nothing unless the directory is a Git repository —
// there is simply nothing to show for a plain folder. The hover card carries the full
// breakdown (commit, author, staged/unstaged/untracked/conflicted counts).
export function GitStatusBar({ status }: { status: DirectoryStatus }) {
  const t = useTranslations("GitStatusBar");
  const format = useFormatter();

  const formatCommitDate = (value: string): string => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return format.dateTime(date, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  if (!status.valid || !status.isGitRepository) return null;

  const branchLabel = status.gitDetached
    ? t("detached", { ref: status.gitShortHead || status.gitBranch || "HEAD" })
    : status.gitBranch || status.gitLabel || "HEAD";
  const changedCount =
    status.gitStagedCount + status.gitUnstagedCount + status.gitUntrackedCount + status.gitConflictedCount;

  const detail = (
    <Box whiteSpace="nowrap">
      <Text fontWeight="semibold" mb={1} color="fg">{branchLabel}</Text>
      <Flex direction="column" ps={2} gap={0.5}>
        {status.gitCommitSubject && <InlineField label={t("commit")}><Text truncate maxW="260px">{status.gitCommitSubject}</Text></InlineField>}
        {status.gitCommitAuthor && <InlineField label={t("author")}><Text>{status.gitCommitAuthor}</Text></InlineField>}
        {status.gitCommitAuthorDate && <InlineField label={t("date")}><Text>{formatCommitDate(status.gitCommitAuthorDate)}</Text></InlineField>}
        {status.gitUpstream && <InlineField label={t("upstream")}><Text>{status.gitUpstream}</Text></InlineField>}
      </Flex>
      {changedCount > 0 && (
        <>
          <Box h="1px" bg="border" my={2} />
          <Flex direction="column" ps={2} gap={0.5}>
            {status.gitStagedCount > 0 && <InlineField label={t("staged")}><Text>{status.gitStagedCount}</Text></InlineField>}
            {status.gitUnstagedCount > 0 && <InlineField label={t("unstaged")}><Text>{status.gitUnstagedCount}</Text></InlineField>}
            {status.gitUntrackedCount > 0 && <InlineField label={t("untracked")}><Text>{status.gitUntrackedCount}</Text></InlineField>}
            {status.gitConflictedCount > 0 && <InlineField label={t("conflicted")}><Text color="red.fg">{status.gitConflictedCount}</Text></InlineField>}
          </Flex>
        </>
      )}
    </Box>
  );

  return (
    <Tooltip
      content={detail}
      rich
      openDelay={200}
      closeDelay={60}
      positioning={{ placement: "bottom" }}
    >
      <Flex align="center" gap={2} flexShrink={0} h={6} px={2} borderRadius="sm" color="fg.muted">
        <Flex align="center" gap={1} flexShrink={0} maxW="200px">
          <LuGitBranch size={13} />
          <Text textStyle="fieldLabel" truncate>{branchLabel}</Text>
        </Flex>
        {status.gitDirty && (
          <Flex align="center" gap={1} flexShrink={0} title={t("uncommittedChanges")}>
            <Box w="7px" h="7px" borderRadius="full" bg="orange.solid" />
            {changedCount > 0 && <Text fontSize="xs" color="orange.fg">{changedCount}</Text>}
          </Flex>
        )}
        {status.gitAhead > 0 && (
          <Flex align="center" gap={0.5} flexShrink={0} color="green.fg" title={t("aheadOfUpstream", { count: status.gitAhead })}>
            <LuArrowUp size={12} /><Text fontSize="xs">{status.gitAhead}</Text>
          </Flex>
        )}
        {status.gitBehind > 0 && (
          <Flex align="center" gap={0.5} flexShrink={0} color="blue.fg" title={t("behindUpstream", { count: status.gitBehind })}>
            <LuArrowDown size={12} /><Text fontSize="xs">{status.gitBehind}</Text>
          </Flex>
        )}
      </Flex>
    </Tooltip>
  );
}
