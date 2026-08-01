/**
 * Every conversation on the machine, grouped by workspace.
 *
 * This is the desktop's sidebar, which on a narrow viewport it already renders as a full-screen
 * view of its own — so the phone is not a new layout so much as the one the web client falls
 * back to, given the whole screen and a push navigator instead of a panel that slides away.
 *
 * The tree is kept: a session created by another session is nested under it, because a peer is an
 * ordinary session and the only thing that says otherwise is where it sits in this list.
 */

import { router } from "expo-router";
import {
  ChevronDown, ChevronRight, Clock, Folder, MessageSquare, Search, Settings, SquarePen, WifiOff,
} from "lucide-react-native";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, RefreshControl, ScrollView, StyleSheet, TextInput, View } from "react-native";

import { FrankMark } from "../components/frank-mark";
import { Button, EmptyState, Row, StatusDot, Text } from "../components/ui";
import {
  fetchSessions, listWorkspaces, subscribeEvents,
  type SessionSummary, type Workspace,
} from "../lib/api";
import { workspaceLabel } from "@shared/workspace";

import { useConnection } from "../lib/connection";
import { useTheme } from "../theme";
import { useEdgeInsets } from "../theme/insets";

/** Shown only when there is something to say — the desktop's rule, and the reason for the nulls. */
function indicatorFor(session: SessionSummary, colors: ReturnType<typeof useTheme>["colors"]): string | null {
  if (session.activity === "working") return colors.fgMuted;
  if (session.awaiting_input || session.activity === "waiting") return colors.yellowSolid;
  if (session.outcome === "failed") return colors.redSolid;
  return null;
}

interface TreeNode {
  session: SessionSummary;
  children: TreeNode[];
}

/** Sessions nested under whatever created them; orphans (a reaped parent) come back to the top. */
function buildTree(sessions: SessionSummary[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>();
  for (const session of sessions) nodes.set(session.id, { session, children: [] });
  const roots: TreeNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.session.parent ? nodes.get(node.session.parent) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

export default function SessionsScreen() {
  const theme = useTheme();
  const insets = useEdgeInsets();
  const { status, reconnect } = useConnection();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [query, setQuery] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const reload = useCallback(async () => {
    if (status !== "online") return;
    try {
      const [foundSessions, foundWorkspaces] = await Promise.all([fetchSessions(), listWorkspaces()]);
      setSessions(foundSessions);
      setWorkspaces(foundWorkspaces);
    } catch {
      // The connection layer owns "can this phone reach the machine"; a failure here is that
      // question being answered elsewhere, and the last list stays on screen meanwhile.
    } finally {
      setLoaded(true);
    }
  }, [status]);

  useEffect(() => {
    // The rule reads `reload` as setting state during the effect, and it does — but only after
    // awaiting the daemon, which the analysis does not follow across. Loading a list when the
    // screen appears is the thing this effect is for.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);

  // The daemon says when something changed rather than being polled, which is what keeps a
  // session started at the desk appearing here without anybody pulling to refresh.
  useEffect(() => {
    if (status !== "online") return;
    return subscribeEvents((event) => {
      if (event === "sessions_changed" || event === "workspaces_changed") void reload();
    });
  }, [status, reload]);

  useEffect(() => {
    if (status === "unpaired") router.replace("/pair");
  }, [status]);

  const grouped = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const visible = needle
      ? sessions.filter((session) =>
          session.title.toLowerCase().includes(needle) || session.agent.toLowerCase().includes(needle))
      : sessions;
    return workspaces.map((workspace) => ({
      workspace,
      roots: buildTree(visible.filter((session) => session.workspace_id === workspace.id)),
    })).filter((group) => group.roots.length > 0 || !needle);
  }, [sessions, workspaces, query]);

  const primaryWorkspace = workspaces[0]?.id ?? "";

  return (
    <View style={[styles.screen, { backgroundColor: theme.colors.bg, paddingTop: insets.top + theme.space[2] }]}>
      <View style={[styles.header, { paddingHorizontal: theme.space[4], gap: theme.space[2] }]}>
        <FrankMark size={24} color={theme.colors.fg} />
        <Text variant="heading" style={{ flex: 1 }}>Frank</Text>
        <Pressable onPress={() => router.push("/schedules")} hitSlop={10}>
          <Clock size={20} color={theme.colors.fgMuted} />
        </Pressable>
        <Pressable onPress={() => router.push("/settings")} hitSlop={10}>
          <Settings size={20} color={theme.colors.fgMuted} />
        </Pressable>
      </View>

      <ConnectionBanner onRetry={reconnect} />

      <View style={{ paddingHorizontal: theme.space[4], paddingTop: theme.space[3], gap: theme.space[2] }}>
        <Button
          label="New conversation" icon={SquarePen} variant="subtle" tone="accent" full
          disabled={status !== "online"}
          onPress={() => router.push({ pathname: "/session/[id]", params: { id: "new", workspace: primaryWorkspace } })}
        />
        <View
          style={[
            styles.search,
            {
              backgroundColor: theme.colors.bgSubtle,
              borderRadius: theme.radii.md,
              paddingHorizontal: theme.space[2.5],
              gap: theme.space[2],
              height: theme.controlHeight,
            },
          ]}
        >
          <Search size={15} color={theme.colors.fgSubtle} />
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder="Search conversations"
            placeholderTextColor={theme.colors.fgSubtle}
            style={[theme.text.body, { flex: 1, color: theme.colors.fg }]}
            autoCapitalize="none"
          />
        </View>
      </View>

      <ScrollView
        contentContainerStyle={{
          paddingHorizontal: theme.space[3],
          paddingBottom: insets.bottom + theme.space[8],
          flexGrow: 1,
        }}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            tintColor={theme.colors.fgMuted}
            onRefresh={() => {
              setRefreshing(true);
              void reload().finally(() => setRefreshing(false));
            }}
          />
        }
      >
        {loaded && grouped.every((group) => group.roots.length === 0) ? (
          <EmptyState
            icon={MessageSquare}
            title={query ? "Nothing matches that" : "No conversations yet"}
            description={query ? undefined : "Start one and it will appear here, on every device paired with this machine."}
          />
        ) : (
          grouped.map((group) => (
            <View key={group.workspace.id} style={{ paddingTop: theme.space[3] }}>
              <Pressable
                onPress={() => setCollapsed((previous) => ({ ...previous, [group.workspace.id]: !previous[group.workspace.id] }))}
                style={[styles.workspaceHeader, { paddingHorizontal: theme.space[1], gap: theme.space[1.5], paddingVertical: theme.space[1.5] }]}
              >
                {collapsed[group.workspace.id]
                  ? <ChevronRight size={14} color={theme.colors.fgSubtle} />
                  : <ChevronDown size={14} color={theme.colors.fgSubtle} />}
                <Folder size={14} color={theme.colors.fgSubtle} />
                <Text variant="sectionLabel" tone="muted" style={{ flex: 1 }} numberOfLines={1}>
                  {workspaceLabel(group.workspace.locations, "en", "Workspace")}
                </Text>
                <Text variant="small" tone="subtle">{group.roots.length}</Text>
              </Pressable>
              {collapsed[group.workspace.id]
                ? null
                : group.roots.map((node) => <SessionBranch key={node.session.id} node={node} depth={0} />)}
            </View>
          ))
        )}
      </ScrollView>
    </View>
  );
}

function SessionBranch({ node, depth }: { node: TreeNode; depth: number }) {
  const theme = useTheme();
  const [open, setOpen] = useState(false);
  const indicator = indicatorFor(node.session, theme.colors);

  return (
    <View style={{ paddingLeft: depth * theme.space[3] }}>
      <Row
        title={node.session.title || "Untitled conversation"}
        subtitle={node.session.agent}
        onPress={() => router.push({ pathname: "/session/[id]", params: { id: node.session.id } })}
        trailing={
          <View style={{ flexDirection: "row", alignItems: "center", gap: theme.space[2] }}>
            {indicator ? <StatusDot color={indicator} /> : null}
            {node.children.length > 0 ? (
              <Pressable onPress={() => setOpen((previous) => !previous)} hitSlop={10}>
                {open
                  ? <ChevronDown size={15} color={theme.colors.fgSubtle} />
                  : <Text variant="small" tone="subtle">{node.children.length}</Text>}
              </Pressable>
            ) : null}
          </View>
        }
      />
      {open ? node.children.map((child) => <SessionBranch key={child.session.id} node={child} depth={depth + 1} />) : null}
    </View>
  );
}

/** Said once, at the top, rather than by every screen that fails to load something. */
function ConnectionBanner({ onRetry }: { onRetry: () => void }) {
  const theme = useTheme();
  const { status, pairing } = useConnection();
  if (status === "online" || status === "unpaired") return null;

  const message = status === "connecting" ? `Looking for ${pairing?.name ?? "your Mac"}…`
    : status === "rejected" ? "That machine no longer recognises this phone. Pair again."
    : `${pairing?.name ?? "Your Mac"} is not answering. It may be asleep, or off this network.`;

  return (
    <View
      style={{
        marginHorizontal: theme.space[4],
        marginTop: theme.space[3],
        padding: theme.space[3],
        borderRadius: theme.radii.md,
        backgroundColor: status === "rejected" ? theme.colors.redSubtle : theme.colors.bgMuted,
        flexDirection: "row",
        alignItems: "center",
        gap: theme.space[2.5],
      }}
    >
      <WifiOff size={16} color={status === "rejected" ? theme.colors.redFg : theme.colors.fgMuted} />
      <Text variant="small" tone={status === "rejected" ? "danger" : "muted"} style={{ flex: 1 }}>{message}</Text>
      {status === "rejected"
        ? <Button label="Pair" variant="outline" tone="danger" onPress={() => router.push("/pair")} />
        : status === "offline" ? <Button label="Retry" variant="outline" onPress={onRetry} /> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1 },
  header: { flexDirection: "row", alignItems: "center" },
  search: { flexDirection: "row", alignItems: "center" },
  workspaceHeader: { flexDirection: "row", alignItems: "center" },
});
