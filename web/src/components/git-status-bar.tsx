"use client";

import { Box, Flex, Separator, Text } from "@chakra-ui/react";
import { useFormatter, useTranslations } from "next-intl";
import { LuArrowDown, LuArrowUp, LuFileDiff, LuGitBranch } from "react-icons/lu";
import { Tooltip } from "./ui/tooltip";
import { InlineField } from "./ui/display";
import type { DirectoryStatus } from "@/lib/use-directory-status";

// The workspace's Git status: branch, working-tree dirtiness and ahead/behind for the
// active working directory. Renders nothing unless the directory is a Git repository —
// there is simply nothing to show for a plain folder. The hover card carries the full
// breakdown (commit, author, staged/unstaged/untracked/conflicted counts).
//
// Every group is one icon and one number at the same size, which is what makes the gaps
// between them read as equal. Dirtiness used to be a bare 8px disc instead — smaller than
// the 12px arrows beside it, and with no shape to sit against the number, so the same
// `gap` measured out to a visibly different space.
export function GitStatusBar({ status }: { status: DirectoryStatus }) {
  const translation = useTranslations("GitStatusBar");
  const format = useFormatter();

  // Spelled out in full. The bar itself is where brevity is worth paying for; the hover card
  // is what someone opens *because* they want the detail, and an abbreviated month there is a
  // saving nobody asked for — the year included, since a commit date is often not this one.
  const formatCommitDate = (value: string): string => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return format.dateTime(date, {
      year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  };

  if (!status.valid || !status.isGitRepository) return null;

  const branchLabel = status.gitDetached
    ? translation("detached", { ref: status.gitShortHead || status.gitBranch || "HEAD" })
    : status.gitBranch || status.gitLabel || "HEAD";
  const changedCount =
    status.gitStagedCount + status.gitUnstagedCount + status.gitUntrackedCount + status.gitConflictedCount;

  const detail = (
    <Box whiteSpace="nowrap">
      {/* The same glyph the bar wears, at the same size and gap, so the card reads as that
          branch rather than as a heading that happens to be a branch name. */}
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuGitBranch size={12} />
        <Text fontWeight="semibold">{branchLabel}</Text>
      </Flex>
      <Flex direction="column" ps={2} gap={1}>
        {status.gitCommitSubject && <InlineField label={translation("commit")}><Text truncate maxW="260px">{status.gitCommitSubject}</Text></InlineField>}
        {status.gitCommitAuthor && <InlineField label={translation("author")}><Text>{status.gitCommitAuthor}</Text></InlineField>}
        {status.gitCommitAuthorDate && <InlineField label={translation("date")}><Text>{formatCommitDate(status.gitCommitAuthorDate)}</Text></InlineField>}
        {status.gitUpstream && <InlineField label={translation("upstream")}><Text>{status.gitUpstream}</Text></InlineField>}
      </Flex>
      {changedCount > 0 && (
        <>
          <Separator my={2} />
          <Flex direction="column" ps={2} gap={1}>
            {status.gitStagedCount > 0 && <InlineField label={translation("staged")}><Text>{status.gitStagedCount}</Text></InlineField>}
            {status.gitUnstagedCount > 0 && <InlineField label={translation("unstaged")}><Text>{status.gitUnstagedCount}</Text></InlineField>}
            {status.gitUntrackedCount > 0 && <InlineField label={translation("untracked")}><Text>{status.gitUntrackedCount}</Text></InlineField>}
            {status.gitConflictedCount > 0 && <InlineField label={translation("conflicted")}><Text color="red.fg">{status.gitConflictedCount}</Text></InlineField>}
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
      <Flex align="center" gap={2} flexShrink={0} h={8} px={2} borderRadius="md" color="fg.muted">
        <Flex align="center" gap={1} flexShrink={0} maxW="200px">
          <LuGitBranch size={12} />
          <Text textStyle="fieldLabel" truncate>{branchLabel}</Text>
        </Flex>
        {status.gitDirty && (
          <Flex align="center" gap={1} flexShrink={0} color="orange.fg" title={translation("uncommittedChanges")}>
            <LuFileDiff size={12} />
            {changedCount > 0 && <Text textStyle="fieldLabel">{changedCount}</Text>}
          </Flex>
        )}
        {status.gitAhead > 0 && (
          <Flex align="center" gap={1} flexShrink={0} color="green.fg" title={translation("aheadOfUpstream", { count: status.gitAhead })}>
            <LuArrowUp size={12} /><Text textStyle="fieldLabel">{status.gitAhead}</Text>
          </Flex>
        )}
        {status.gitBehind > 0 && (
          <Flex align="center" gap={1} flexShrink={0} color="blue.fg" title={translation("behindUpstream", { count: status.gitBehind })}>
            <LuArrowDown size={12} /><Text textStyle="fieldLabel">{status.gitBehind}</Text>
          </Flex>
        )}
      </Flex>
    </Tooltip>
  );
}
