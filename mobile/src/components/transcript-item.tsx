/**
 * One row of a conversation.
 *
 * The desktop's grammar, kept: the person's messages are bubbles, the agent's prose is not. A
 * bubble on both sides makes a transcript read as a chat between equals, and the agent is not
 * chatting — it is answering, at length, with code in it. The bare text is also the only shape
 * that lets a fenced block use the full width of a phone.
 *
 * Tool calls are activity *lines*, never cards, and a contiguous run of them collapses to one.
 * That is a nicety on a laptop and the difference between usable and not on a phone, where
 * twelve file reads would otherwise be the entire screen.
 */

import {
  ChevronDown, ChevronRight, CircleAlert, Layers, Sparkles, TriangleAlert, Users,
} from "lucide-react-native";
import { memo, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, View } from "react-native";

import { toolDisplay } from "../lib/tool-display";
import type { ChatMessage, TimelineItem, ToolCallMessage } from "../lib/transcript";
import { useTheme } from "../theme";
import { Markdown } from "./markdown";
import { Pill, Text } from "./ui";

export const TranscriptItem = memo(function TranscriptItem({
  item, streaming,
}: {
  item: TimelineItem;
  streaming: boolean;
}) {
  if (item.kind === "tools") return <ToolGroup calls={item.calls} thinking={item.thinking} />;
  return <Message message={item.message} streaming={streaming} />;
});

function Message({ message, streaming }: { message: ChatMessage; streaming: boolean }) {
  const theme = useTheme();

  switch (message.role) {
    case "user":
      return (
        <View
          style={[
            styles.bubble,
            {
              backgroundColor: theme.colors.bgMuted,
              borderColor: theme.colors.border,
              borderRadius: theme.radii.md,
              paddingHorizontal: theme.space[2.5],
              paddingVertical: theme.space[2],
            },
          ]}
        >
          <Text variant="body">{message.text}</Text>
        </View>
      );

    case "peer":
      return (
        <View
          style={{
            backgroundColor: theme.colors.bgSubtle,
            borderLeftColor: theme.colors.borderEmphasized,
            borderLeftWidth: 2,
            paddingHorizontal: theme.space[3],
            paddingVertical: theme.space[2],
            borderRadius: theme.radii.sm,
            gap: theme.space[1],
          }}
        >
          <View style={{ flexDirection: "row", alignItems: "center", gap: theme.space[1.5] }}>
            <Users size={12} color={theme.colors.fgSubtle} />
            <Text variant="small" tone="subtle">From {message.sender || "a peer session"}</Text>
          </View>
          <Text variant="body">{message.text}</Text>
        </View>
      );

    case "assistant":
      return <Markdown text={message.text} streaming={streaming} />;

    case "thinking":
      return <Thinking message={message} />;

    case "tool":
      // Reached only for a tool call that is alone between two other kinds of row; a run of them
      // arrives as a group.
      return <ToolGroup calls={[message]} thinking={[]} />;

    case "steering":
      return (
        <View style={[styles.steering, { gap: theme.space[1.5], alignSelf: "flex-end", maxWidth: "88%" }]}>
          <Pill label="Sent mid-turn" tone="accent" />
          <View
            style={{
              backgroundColor: theme.colors.bgMuted,
              borderRadius: theme.radii.md,
              paddingHorizontal: theme.space[2.5],
              paddingVertical: theme.space[1.5],
              opacity: 0.85,
            }}
          >
            <Text variant="small">{message.text}</Text>
          </View>
        </View>
      );

    case "error":
      return (
        <View
          style={[
            styles.notice,
            {
              backgroundColor: theme.colors.redSubtle,
              borderColor: theme.colors.redMuted,
              borderRadius: theme.radii.md,
              padding: theme.space[3],
              gap: theme.space[1.5],
            },
          ]}
        >
          <View style={{ flexDirection: "row", alignItems: "center", gap: theme.space[1.5] }}>
            <TriangleAlert size={15} color={theme.colors.redFg} />
            <Text variant="title" tone="danger">{message.title}</Text>
          </View>
          {message.message ? <Text variant="small" tone="danger">{message.message}</Text> : null}
        </View>
      );

    case "warning":
      return (
        <View
          style={[
            styles.notice,
            {
              backgroundColor: theme.colors.orangeSubtle,
              borderColor: theme.colors.orangeSolid,
              borderRadius: theme.radii.md,
              padding: theme.space[3],
              gap: theme.space[1.5],
            },
          ]}
        >
          <View style={{ flexDirection: "row", alignItems: "center", gap: theme.space[1.5] }}>
            <CircleAlert size={15} color={theme.colors.orangeFg} />
            <Text variant="title" tone="warning">{message.title}</Text>
          </View>
          {message.message ? <Text variant="small" tone="warning">{message.message}</Text> : null}
        </View>
      );

    case "compaction":
      return (
        <View style={[styles.divider, { gap: theme.space[2.5], paddingVertical: theme.space[1] }]}>
          <View style={{ flex: 1, height: 1, backgroundColor: theme.colors.borderMuted }} />
          <Layers size={12} color={theme.colors.fgSubtle} />
          <Text variant="small" tone="subtle">
            {message.status === "done" ? "Earlier turns folded up" : "Folding earlier turns…"}
          </Text>
          <View style={{ flex: 1, height: 1, backgroundColor: theme.colors.borderMuted }} />
        </View>
      );
  }
}

function Thinking({ message }: { message: Extract<ChatMessage, { role: "thinking" }> }) {
  const theme = useTheme();
  const [open, setOpen] = useState(false);
  return (
    <View>
      <Pressable
        onPress={() => setOpen((previous) => !previous)}
        style={[styles.line, { gap: theme.space[2], paddingVertical: theme.space[1] }]}
      >
        {message.running
          ? <ActivityIndicator size="small" color={theme.colors.fgSubtle} />
          : <Sparkles size={14} color={theme.colors.fgSubtle} />}
        <Text variant="small" tone="subtle" style={{ flex: 1 }}>
          {message.running ? "Thinking…" : "Thought about it"}
        </Text>
        {message.text
          ? (open ? <ChevronDown size={14} color={theme.colors.fgSubtle} /> : <ChevronRight size={14} color={theme.colors.fgSubtle} />)
          : null}
      </Pressable>
      {open && message.text ? (
        <View style={[styles.rail, { borderLeftColor: theme.colors.borderMuted, paddingLeft: theme.space[3.5], marginLeft: theme.space[1.5] }]}>
          <Text variant="small" tone="muted">{message.text}</Text>
        </View>
      ) : null}
    </View>
  );
}

function ToolGroup({ calls, thinking }: { calls: ToolCallMessage[]; thinking: ChatMessage[] }) {
  const theme = useTheme();
  const [open, setOpen] = useState(false);
  const running = calls.some((call) => call.status === "running");
  const failed = calls.filter((call) => call.status === "error").length;
  const lead = calls[calls.length - 1] ?? calls[0];
  const display = toolDisplay(lead.name, lead.arguments);
  const Icon = display.icon;

  return (
    <View>
      <Pressable
        onPress={() => setOpen((previous) => !previous)}
        style={[styles.line, { gap: theme.space[2], paddingVertical: theme.space[1] }]}
      >
        {running
          ? <ActivityIndicator size="small" color={theme.colors.fgSubtle} />
          : <Icon size={14} color={theme.colors[display.tint] as string} />}
        <Text
          variant="small"
          tone={failed ? "danger" : "muted"}
          numberOfLines={open ? undefined : 1}
          style={[{ flex: 1 }, display.mono ? theme.text.mono : null]}
        >
          {display.label}
        </Text>
        {calls.length > 1 ? <Pill label={`${calls.length}`} /> : null}
        {failed ? <Pill label={failed === calls.length ? "failed" : `${failed} failed`} tone="danger" /> : null}
        {open ? <ChevronDown size={14} color={theme.colors.fgSubtle} /> : <ChevronRight size={14} color={theme.colors.fgSubtle} />}
      </Pressable>

      {open ? (
        <View
          style={[
            styles.rail,
            { borderLeftColor: theme.colors.borderMuted, paddingLeft: theme.space[3.5], marginLeft: theme.space[1.5], gap: theme.space[1.5], paddingVertical: theme.space[1] },
          ]}
        >
          {thinking.map((entry) => (
            <Text key={entry.key} variant="small" tone="subtle">
              {entry.role === "thinking" ? entry.text : ""}
            </Text>
          ))}
          {calls.map((call) => <ToolDetail key={call.key} call={call} />)}
        </View>
      ) : null}
    </View>
  );
}

function ToolDetail({ call }: { call: ToolCallMessage }) {
  const theme = useTheme();
  const display = toolDisplay(call.name, call.arguments);
  const Icon = display.icon;
  // The tools that carry a body worth reading put it in one of these, and the rest are described
  // well enough by their label. Showing the whole argument object instead would be a wall of
  // JSON on a screen four inches wide.
  const body = String(call.arguments.command ?? call.arguments.script ?? call.arguments.content ?? "");

  return (
    <View style={{ gap: theme.space[1] }}>
      <View style={[styles.line, { gap: theme.space[1.5] }]}>
        <Icon size={12} color={theme.colors[display.tint] as string} />
        <Text variant="small" tone="muted" style={{ flex: 1 }} numberOfLines={2}>{call.name}</Text>
        {call.durationMs != null ? (
          <Text variant="small" tone="subtle">{formatDuration(call.durationMs)}</Text>
        ) : null}
        {call.status === "error" ? <Pill label="failed" tone="danger" /> : null}
      </View>
      {body ? (
        <View
          style={{
            backgroundColor: theme.colors.bgSubtle,
            borderColor: theme.colors.borderMuted,
            borderWidth: 1,
            borderRadius: theme.radii.sm,
            padding: theme.space[2],
          }}
        >
          <Text variant="mono" tone="muted" numberOfLines={12}>{body.trim()}</Text>
        </View>
      ) : null}
    </View>
  );
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)}s`;
  return `${Math.round(milliseconds / 60_000)}m`;
}

const styles = StyleSheet.create({
  bubble: { alignSelf: "flex-end", maxWidth: "88%", borderWidth: 1 },
  line: { flexDirection: "row", alignItems: "center" },
  rail: { borderLeftWidth: 2 },
  notice: { borderWidth: 1 },
  divider: { flexDirection: "row", alignItems: "center" },
  steering: { alignItems: "flex-end" },
});
