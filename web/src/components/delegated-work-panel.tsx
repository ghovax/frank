"use client";

// The work the open conversation handed to other sessions.
//
// A session composes by creating peers: it makes a second session, briefs it, and gets an
// answer back as a message. Those peers are ordinary sessions, and for a while they were
// ordinary rows in the sidebar — which is wrong twice over. Nested there, one task that fanned
// out showed a single row where eight conversations existed. Listed flat, the sidebar filled
// with rows nobody started and could not place.
//
// So the sidebar lists what you began, and this lists what the one you picked went and did. The
// split is the point: the sidebar answers "which conversation do I want", and this answers
// "what is happening underneath it". They nest — the sidebar's selection is the first choice,
// and this panel is always about that choice, so opening a delegated session here reads its
// transcript without moving the selection above it.

import { VStack } from "@chakra-ui/react";
import { useCallback, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { LuGitBranch } from "react-icons/lu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { PanelBody, PanelCard, PanelEmptyState, PanelHeader } from "@/components/ui/panel";
import { Pill } from "@/components/ui/pill";
import type { AgentSummary } from "@/lib/api";
import { SessionRow, type SessionEntry } from "./session-row";

// A session and everything it created. The daemon hands the registry out flat (each row
// carrying its parent), so the nesting is derived here rather than being a shape every other
// consumer would have to flatten again to search or sort.
interface SessionTreeNode {
  entry: SessionEntry;
  children: SessionTreeNode[];
}

function buildSessionTree(entries: SessionEntry[]): SessionTreeNode[] {
  const nodes = new Map(entries.map((entry) => [entry.sessionId, { entry, children: [] as SessionTreeNode[] }]));
  const roots: SessionTreeNode[] = [];
  for (const node of nodes.values()) {
    // A child whose parent is not in this list (living in another workspace, or deleted out
    // from under it) is promoted to a root rather than dropped — a session is never
    // unreachable because of where its parent happens to be.
    const parent = node.entry.parentSessionId ? nodes.get(node.entry.parentSessionId) : undefined;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  // Children oldest first, so a fan-out reads down the page in the order it happened — the
  // only order in which a delegation is legible. Roots keep the order they arrived in, which
  // is the sidebar's: most recent conversation at the top.
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
  // Open unless the reader has closed it. A panel opened to see what a conversation delegated
  // and then hiding it behind a chevron on every row would be asking for the same click twice.
  const expanded = hasChildren && !collapsedSessions.has(node.entry.sessionId);
  const below = hasChildren ? descendants(node) : [];
  // What a closed group is holding. Without this the panel would swallow exactly the signals
  // it exists to raise — a child parked on a permission prompt, or one that crashed.
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
      badges={!expanded && hasChildren ? (
        <Pill colorPalette={failed > 0 ? "red" : waiting > 0 ? "yellow" : "gray"}>{below.length}</Pill>
      ) : undefined}
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
  // Every session the client knows about, in the sidebar's order. The subtree is derived here
  // rather than by the caller, so the panel and the sidebar cannot come to disagree about which
  // sessions belong under the conversation you have selected.
  sessions: SessionEntry[];
  // The conversation this panel is about: the sidebar's selection. Its delegated sessions are
  // what the panel lists.
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

  // The selected conversation *and* what it delegated, as the tree it forms.
  //
  // Its own row is here, at the top, with everything under it. Leaving it out was the tidier
  // idea — the sidebar already lights that row — and it made the panel unusable for the thing it
  // is for: you could move between the delegated sessions but not back to the one that created
  // them, so returning to the parent meant leaving the panel and finding it in the sidebar.
  // A tree missing its root is a list of orphans.
  const root = useMemo(() => {
    if (!rootSessionId) return undefined;
    // A delegated session opened directly is not a root of the whole forest, so find it wherever
    // it sits: the panel stays scoped to the conversation, not to the row clicked.
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
        onOpenChange={(open) => { if (!open) setPendingDelete(null); }}
        title={sidebarTranslation("deleteTitle")}
        confirmLabel={sidebarTranslation("deleteConfirm")}
        danger
        onConfirm={() => { if (pendingDelete) onDeleteSession(pendingDelete); }}
      >
        {sidebarTranslation("deleteBody", { title: pendingDelete?.title || sidebarTranslation("untitledConversation") })}
      </ConfirmDialog>
    </PanelCard>
  );
}
