"use client";

import { useEffect, useState } from "react";
import { subscribeGitStatus, validateWorkingDirectory, type DirectoryValidation } from "@/lib/api";

// Live status of a working directory: whether it is a valid path and — when it is a Git repository — its branch, dirtiness, ahead/behind and current commit, streamed so the workspace status bar stays current as the tree changes.
export type DirectoryStatus = {
  path: string;
  valid: boolean;
  checking: boolean;
  isGitRepository: boolean;
  repositoryRoot: string;
  gitBranch: string;
  gitShortHead: string;
  gitDirty: boolean;
  gitDetached: boolean;
  gitLabel: string;
  gitCommitSubject: string;
  gitCommitAuthor: string;
  gitCommitAuthorDate: string;
  gitUpstream: string;
  gitAhead: number;
  gitBehind: number;
  gitStagedCount: number;
  gitUnstagedCount: number;
  gitUntrackedCount: number;
  gitConflictedCount: number;
};

const EMPTY_GIT = {
  valid: false,
  isGitRepository: false,
  repositoryRoot: "",
  gitBranch: "",
  gitShortHead: "",
  gitDirty: false,
  gitDetached: false,
  gitLabel: "",
  gitCommitSubject: "",
  gitCommitAuthor: "",
  gitCommitAuthorDate: "",
  gitUpstream: "",
  gitAhead: 0,
  gitBehind: 0,
  gitStagedCount: 0,
  gitUnstagedCount: 0,
  gitUntrackedCount: 0,
  gitConflictedCount: 0,
} as const;

function fromValidation(path: string, result: DirectoryValidation): DirectoryStatus {
  return {
    path,
    checking: false,
    valid: result.valid,
    isGitRepository: result.is_git_repository,
    repositoryRoot: result.repository_root,
    gitBranch: result.git_branch,
    gitShortHead: result.git_short_head,
    gitDirty: result.git_dirty,
    gitDetached: result.git_detached,
    gitLabel: result.git_label,
    gitCommitSubject: result.git_commit_subject,
    gitCommitAuthor: result.git_commit_author,
    gitCommitAuthorDate: result.git_commit_author_date,
    gitUpstream: result.git_upstream,
    gitAhead: result.git_ahead,
    gitBehind: result.git_behind,
    gitStagedCount: result.git_staged_count,
    gitUnstagedCount: result.git_unstaged_count,
    gitUntrackedCount: result.git_untracked_count,
    gitConflictedCount: result.git_conflicted_count,
  };
}

export function useDirectoryStatus(workingDirectory?: string): { status: DirectoryStatus; directoryValid: boolean } {
  const directory = (workingDirectory ?? "").trim();
  const [status, setStatus] = useState<DirectoryStatus>({ path: directory, checking: !!directory, ...EMPTY_GIT });

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | null = null;

    // Debounced so rapid working-directory changes (e.g. switching sessions) don't fire a validation request per keystroke of navigation.
    const timeout = window.setTimeout(() => {
      if (!directory) {
        setStatus({ path: directory, checking: false, ...EMPTY_GIT });
        return;
      }
      setStatus({ path: directory, checking: true, ...EMPTY_GIT });
      validateWorkingDirectory(directory)
        .then((result) => {
          if (cancelled) return;
          setStatus(fromValidation(directory, result));
          // Only a Git repository has live status worth streaming.
          if (!result.is_git_repository) return;
          unsubscribe = subscribeGitStatus(directory, (live) => {
            if (!cancelled) setStatus(fromValidation(directory, live));
          });
        })
        .catch(() => {
          if (!cancelled) setStatus({ path: directory, checking: false, ...EMPTY_GIT });
        });
    }, 250);

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
      unsubscribe?.();
    };
  }, [directory]);

  // Valid only once the streamed/validated status matches the directory being asked about — during the switch window the stale status still names the previous path.
  const directoryValid = !!directory && status.path === directory && status.valid;
  return { status, directoryValid };
}
