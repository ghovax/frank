/**
 * One conversation.
 *
 * The route takes a session id, or the literal `new` for one that does not exist yet. That is
 * deliberate rather than a second screen: a fresh conversation is the same surface with an empty
 * transcript, and the session is only actually created when the first message is sent — which is
 * how the desktop behaves and why a chat you opened and abandoned leaves nothing behind.
 */

import { useLocalSearchParams } from "expo-router";
import { ChevronLeft, Layers, MessageSquare, MoreVertical, Trash2 } from "lucide-react-native";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, View,
} from "react-native";

import { Composer } from "../../components/composer";
import { PermissionGate, QuestionGate } from "../../components/gates";
import { Sheet } from "../../components/sheet";
import { TranscriptItem } from "../../components/transcript-item";
import { Button, EmptyState, Row, Text } from "../../components/ui";
import {
  deleteSession, fetchSession, listWorkspaces,
  type PermissionMode, type SessionSummary,
} from "../../lib/api";
import { useConnection } from "../../lib/connection";
import { goBack } from "../../lib/navigation";
import { timelineItems } from "../../lib/transcript";
import { useChat } from "../../lib/use-chat";
import { useTheme } from "../../theme";
import { useEdgeInsets } from "../../theme/insets";

/**
 * The route, whose only job is to make the conversation below a fresh component per session.
 *
 * The `key` is the whole point. Navigating from one conversation to another reuses this screen,
 * and a transcript that emptied and refilled itself in place had to reset half a dozen pieces of
 * state in an effect — which is both more code and a render that immediately schedules another.
 * Keyed, a different conversation is a different component, and "start empty" is just what
 * mounting means.
 */
export default function ConversationRoute() {
  const parameters = useLocalSearchParams<{ id: string; workspace?: string }>();
  const routeId = String(parameters.id);
  return <Conversation key={routeId} routeId={routeId} workspaceParameter={String(parameters.workspace ?? "")} />;
}

function Conversation({ routeId, workspaceParameter }: { routeId: string; workspaceParameter: string }) {
  const theme = useTheme();
  const insets = useEdgeInsets();
  const { status } = useConnection();
  const isNew = routeId === "new";
  const sessionId = isNew ? null : routeId;

  const [existing, setExisting] = useState<SessionSummary | null>(null);
  const [workspaceId, setWorkspaceId] = useState(workspaceParameter);
  const [agent, setAgent] = useState("general-assistant");
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("default");
  const [menu, setMenu] = useState(false);

  // A session that already exists carries its own agent and mode; the composer shows them and
  // will not let them be changed, because the daemon fixed them at creation.
  useEffect(() => {
    if (!sessionId || status !== "online") return;
    fetchSession(sessionId)
      .then((found) => {
        if (!found) return;
        setExisting(found);
        setAgent(found.agent);
        setPermissionMode(found.permission_mode);
        setWorkspaceId(found.workspace_id);
      })
      .catch(() => {});
  }, [sessionId, status]);

  // A conversation started from this screen needs somewhere to run. The first workspace is the
  // right default and the only one a phone can sensibly offer: choosing a folder is a thing you
  // do at the machine, and the workspaces the machine already has are that choice, made.
  useEffect(() => {
    if (!isNew || workspaceId || status !== "online") return;
    listWorkspaces().then((found) => { if (found[0]) setWorkspaceId(found[0].id); }).catch(() => {});
  }, [isNew, workspaceId, status]);

  const chat = useChat({
    agent,
    sessionId,
    workingDirectory: existing?.working_directory ?? "",
    workspaceId,
    permissionMode,
    running: existing?.activity === "working",
    ready: status === "online",
  });

  const items = useMemo(() => timelineItems(chat.messages), [chat.messages]);

  const scroller = useRef<ScrollView>(null);
  const pinned = useRef(true);
  useEffect(() => {
    // Only when the reader is already at the bottom. Scrolling somebody back down while they are
    // reading something further up is the single most irritating thing a live transcript can do.
    if (pinned.current) scroller.current?.scrollToEnd({ animated: true });
  }, [items.length, chat.messages]);

  const title = existing?.title || (isNew ? "New conversation" : "Conversation");

  const remove = useCallback(async () => {
    setMenu(false);
    if (chat.sessionId) await deleteSession(chat.sessionId);
    goBack();
  }, [chat.sessionId]);

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: theme.colors.bg }}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={0}
    >
      <View style={[styles.bar, { paddingTop: insets.top + theme.space[1], paddingHorizontal: theme.space[2], gap: theme.space[1] }]}>
        <Pressable onPress={() => goBack()} hitSlop={10} style={{ padding: theme.space[1] }}>
          <ChevronLeft size={22} color={theme.colors.fg} />
        </Pressable>
        <View style={{ flex: 1 }}>
          <Text variant="title" numberOfLines={1}>{title}</Text>
          {existing ? (
            <Text variant="small" tone="subtle" numberOfLines={1}>
              {existing.agent} · {folderOf(existing.working_directory)}
            </Text>
          ) : null}
        </View>
        {chat.streaming ? <ActivityIndicator size="small" color={theme.colors.fgMuted} /> : null}
        <Pressable onPress={() => setMenu(true)} hitSlop={10} style={{ padding: theme.space[1] }}>
          <MoreVertical size={20} color={theme.colors.fgMuted} />
        </Pressable>
      </View>

      <ScrollView
        ref={scroller}
        style={{ flex: 1 }}
        contentContainerStyle={{
          padding: theme.space[3],
          gap: theme.space[2.5],
          flexGrow: 1,
        }}
        onScroll={({ nativeEvent }) => {
          const distance = nativeEvent.contentSize.height
            - nativeEvent.layoutMeasurement.height - nativeEvent.contentOffset.y;
          pinned.current = distance < 120;
        }}
        scrollEventThrottle={64}
        keyboardDismissMode="interactive"
      >
        {chat.loadingHistory ? (
          <View style={styles.center}><ActivityIndicator color={theme.colors.fgMuted} /></View>
        ) : chat.historyError ? (
          <EmptyState icon={MessageSquare} title="This conversation would not load" description={chat.historyError} />
        ) : items.length === 0 ? (
          <EmptyState
            icon={MessageSquare}
            title={isNew ? "What should we work on?" : "Nothing here yet"}
            description={isNew ? "Whatever you send starts a session on your Mac." : undefined}
          />
        ) : (
          <>
            {chat.hasOlder ? (
              <Button label="Load earlier" variant="ghost" onPress={() => void chat.loadOlder()} />
            ) : null}
            {items.map((item) => (
              <TranscriptItem
                key={item.kind === "tools" ? item.key : item.message.key}
                item={item}
                streaming={chat.streaming}
              />
            ))}
          </>
        )}
      </ScrollView>

      <View
        style={{
          paddingHorizontal: theme.space[3],
          paddingTop: theme.space[2],
          paddingBottom: insets.bottom + theme.space[2],
          borderTopColor: theme.colors.borderMuted,
          borderTopWidth: 1,
          gap: theme.space[2.5],
        }}
      >
        {chat.permission ? (
          <PermissionGate request={chat.permission} onAnswer={(decision) => void chat.answerPermission(decision)} />
        ) : null}
        {chat.question ? (
          <QuestionGate request={chat.question} onAnswer={(answers, declined) => void chat.answerQuestion(answers, declined)} />
        ) : null}

        {chat.queued.map((entry, index) => (
          <Pressable
            key={`${entry}-${index}`}
            onPress={() => chat.dropQueued(index)}
            style={{
              alignSelf: "flex-end",
              maxWidth: "88%",
              borderStyle: "dashed",
              borderWidth: 1,
              borderColor: theme.colors.border,
              borderRadius: theme.radii.md,
              paddingHorizontal: theme.space[2.5],
              paddingVertical: theme.space[1.5],
              opacity: 0.7,
            }}
          >
            <Text variant="small" numberOfLines={2}>{entry}</Text>
          </Pressable>
        ))}

        <Composer
          onSend={(text) => chat.send(text, !!chat.permission || !!chat.question)}
          onStop={() => void chat.stop()}
          streaming={chat.streaming}
          queueOnly={!!chat.permission || !!chat.question}
          disabled={status !== "online"}
          locked={!!chat.sessionId}
          agent={agent}
          onAgentChange={setAgent}
          permissionMode={permissionMode}
          onPermissionModeChange={setPermissionMode}
          workingDirectory={existing?.working_directory ?? ""}
          usage={chat.usage}
        />
      </View>

      <Sheet open={menu} onClose={() => setMenu(false)} title={title}>
        <Row
          icon={Layers}
          title="Fold up earlier turns"
          subtitle="Summarise the conversation so far to free context."
          onPress={() => { setMenu(false); void chat.compact(); }}
        />
        <Row
          icon={Trash2}
          title="End this conversation"
          subtitle="The session and everything it created are reaped."
          tone="danger"
          onPress={() => void remove()}
        />
      </Sheet>
    </KeyboardAvoidingView>
  );
}

function folderOf(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? path;
}

const styles = StyleSheet.create({
  bar: { flexDirection: "row", alignItems: "center" },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
});
