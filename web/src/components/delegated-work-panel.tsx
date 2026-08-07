"use client";

// The work the open conversation handed to other sessions.

import { VStack } from "@chakra-ui/react";
import { useCallback, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { LuGitBranch } from "react-icons/lu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { PanelBody, PanelCard, PanelEmptyState, PanelHeader } from "@/components/ui/panel";
import { Pill } from "@/components/ui/pill";
import type { AgentSummary } from "@/lib/api";
import { SessionRow, type SessionEntry } from "./session-row";

// A session and everything it created, nested here because the daemon hands the registry out flat.
interface SessionTreeNode {
  entry: SessionEntry;
  children: SessionTreeNode[];
}

function buildSessionTree(entries: SessionEntry[]): SessionTreeNode[] {
  const nodes = new Map(
    entries.map((entry) => [entry.sessionId, { entry, children: [] as SessionTreeNode[] }]),
  );
  const roots: SessionTreeNode[] = [];
  for (const node of nodes.values()) {
    // A child whose parent is not in this list is promoted to a root rather than dropped.
    const parent = node.entry.parentSessionId ? nodes.get(node.entry.parentSessionId) : undefined;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  // Children oldest first, so a fan-out reads down the page in the order it happened.
  for (const node of nodes.values()) {
    node.children.sort((left, right) => left.entry.createdAt.localeCompare(right.entry.createdAt));
  }
  return roots;
}

function findNode(nodes: SessionTreeNode[], sessionId: string): SessionTreeNode | undefined {
  for (const node of nodes) {
    if (node.entry.sessionId === sessionId) return node;
    const found = findNode(node.children, sessionId);
    if (found) return found;
  }
  return undefined;
}

// Every session below this one, not counting itself.
function descendants(node: SessionTreeNode): SessionEntry[] {
  return node.children.flatMap((child) => [child.entry, ...descendants(child)]);
}

function DelegationBranch({
  node,
  agents,
  activeSessionId,
  unseenCompletions,
  collapsedSessions,
  onToggleCollapsed,
  onResume,
  onRequestDelete,
}: {
  node: SessionTreeNode;
  agents: AgentSummary[];
  activeSessionId: string | null;
  unseenCompletions: Set<string>;
  collapsedSessions: Set<string>;
  onToggleCollapsed: (sessionId: string) => void;
  onResume: (entry: SessionEntry) => void;
  onRequestDelete: (entry: SessionEntry) => void;
}) {
  const hasChildren = node.children.length > 0;
  // Open unless the reader has closed it, since the panel exists to show exactly this.
  const expanded = hasChildren && !collapsedSessions.has(node.entry.sessionId);
  const below = hasChildren ? descendants(node) : [];
  // What a closed group is holding, so the panel does not swallow the signals it exists to raise.
  const waiting = below.filter((child) => child.awaitingInput).length;
  const failed = below.filter((child) => child.failed).length;

  return (
    <SessionRow
      entry={node.entry}
      agents={agents}
      isActive={node.entry.sessionId === activeSessionId}
      unseenCompletions={unseenCompletions}
      disclosure={hasChildren ? (expanded ? "open" : "closed") : undefined}
      onDisclosureChange={() => onToggleCollapsed(node.entry.sessionId)}
      badges={
        !expanded && hasChildren ? (
          <Pill colorPalette={failed > 0 ? "red" : waiting > 0 ? "yellow" : "gray"}>
            {below.length}
          </Pill>
        ) : undefined
      }
      onResume={onResume}
      onRequestDelete={onRequestDelete}
    >
      <VStack gap={1} align="stretch">
        {node.children.map((child) => (
          <DelegationBranch
            key={child.entry.sessionId}
            node={child}
            agents={agents}
            activeSessionId={activeSessionId}
            unseenCompletions={unseenCompletions}
            collapsedSessions={collapsedSessions}
            onToggleCollapsed={onToggleCollapsed}
            onResume={onResume}
            onRequestDelete={onRequestDelete}
          />
        ))}
      </VStack>
    </SessionRow>
  );
}

export function DelegatedWorkPanel({
  sessions,
  rootSessionId,
  activeSessionId,
  unseenCompletions,
  agents,
  onResume,
  onDeleteSession,
  onClose,
}: {
  // Every session the client knows, in the sidebar's order, with the subtree derived here.
  sessions: SessionEntry[];
  // The conversation this panel is about, whose delegated sessions it lists.
  rootSessionId: string | null;
  activeSessionId: string | null;
  unseenCompletions: Set<string>;
  agents: AgentSummary[];
  onResume: (entry: SessionEntry) => void;
  onDeleteSession: (entry: SessionEntry) => void;
  onClose: () => void;
}) {
  const translation = useTranslations("DelegatedWorkPanel");
  const sidebarTranslation = useTranslations("SessionsSidebar");
  const [collapsedSessions, setCollapsedSessions] = useState<Set<string>>(() => new Set());
  const [pendingDelete, setPendingDelete] = useState<SessionEntry | null>(null);

  const toggleCollapsed = useCallback((sessionId: string) => {
    setCollapsedSessions((current) => {
      const next = new Set(current);
      if (!next.delete(sessionId)) next.add(sessionId);
      return next;
    });
  }, []);

  // The selected conversation and what it delegated, as the tree it forms, with its own row at the top.
  const root = useMemo(() => {
    if (!rootSessionId) return undefined;
    // A delegated session opened directly is not a root, so it is found wherever it sits.
    return findNode(buildSessionTree(sessions), rootSessionId);
  }, [sessions, rootSessionId]);

  return (
    <PanelCard>
      <PanelHeader
        icon={<LuGitBranch size={14} />}
        title={translation("title")}
        onClose={onClose}
        closeLabel={translation("collapsePanel")}
      />

      <PanelBody pt={1}>
        {!root || root.children.length === 0 ? (
          <PanelEmptyState
            icon={<LuGitBranch />}
            title={translation("emptyTitle")}
            description={translation("emptyDescription")}
          />
        ) : (
          <VStack gap={1} align="stretch">
            <DelegationBranch
              node={root}
              agents={agents}
              activeSessionId={activeSessionId}
              unseenCompletions={unseenCompletions}
              collapsedSessions={collapsedSessions}
              onToggleCollapsed={toggleCollapsed}
              onResume={onResume}
              onRequestDelete={setPendingDelete}
            />
          </VStack>
        )}
      </PanelBody>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={sidebarTranslation("deleteTitle")}
        confirmLabel={sidebarTranslation("deleteConfirm")}
        danger
        onConfirm={() => {
          if (pendingDelete) onDeleteSession(pendingDelete);
        }}
      >
        {sidebarTranslation("deleteBody", {
          title: pendingDelete?.title || sidebarTranslation("untitledConversation"),
        })}
      </ConfirmDialog>
    </PanelCard>
  );
}
