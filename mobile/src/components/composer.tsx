/**
 * The composer.
 *
 * The desktop's version drops labels as its container narrows — provider name, then the sandbox
 * label, then the permission label, then the counts — on the rule that it is better to drop a
 * label than to truncate it. A phone is below the last of those breakpoints before it starts, so
 * this is that ladder's final rung: icons and values, no words, and the words live in the sheet
 * that opens when you tap one.
 *
 * The microphone is the control that differs most from the desktop's, and only underneath. The
 * button behaves identically — press to record, press to stop, the text is *appended* to what is
 * already typed rather than replacing it — but the recording is a file on disk rather than a tap
 * on an audio graph. See `lib/dictation.ts`.
 */

import * as Haptics from "expo-haptics";
import {
  ArrowUp, BadgeCheck, Bot, Eye, Hand, Mic, MicOff, Square,
} from "lucide-react-native";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Platform, Pressable, StyleSheet, TextInput, View } from "react-native";

import {
  fetchAgents, fetchDictationStatus, transcribeDictation,
  type AgentSummary, type DictationStatus, type PermissionMode,
} from "../lib/api";
import {
  DICTATION_RECORDING, DICTATION_SUPPORTED, dictationUnavailableReason, requestMicrophone,
  samplesFrom, useAudioRecorder,
} from "../lib/dictation";
import type { TokenUsage } from "../lib/transcript";
import { useTheme } from "../theme";
import { Sheet } from "./sheet";
import { Pill, Row, Text } from "./ui";

const PERMISSION_MODES: { value: PermissionMode; label: string; description: string }[] = [
  { value: "default", label: "Ask for approval", description: "Frank stops and asks before anything risky." },
  { value: "auto", label: "Approve for me", description: "Frank decides for itself. Nothing is asked." },
  { value: "read_only", label: "Approve read-only", description: "Reading is automatic; anything that writes is asked." },
];

const PERMISSION_ICON = { default: Hand, auto: BadgeCheck, read_only: Eye } as const;

/** About six lines. Past that the message scrolls, so the transcript is not pushed off screen. */
const COMPOSER_MAXIMUM_HEIGHT = 140;

export function Composer({
  onSend, onStop, streaming, queueOnly, disabled, locked,
  agent, onAgentChange, permissionMode, onPermissionModeChange, workingDirectory, usage,
}: {
  onSend: (text: string) => void;
  onStop: () => void;
  streaming: boolean;
  /** The session is parked on a decision, so anything typed waits rather than steers. */
  queueOnly: boolean;
  disabled: boolean;
  /** A session that has already started: its agent and mode were fixed at creation. */
  locked: boolean;
  agent: string;
  onAgentChange: (agent: string) => void;
  permissionMode: PermissionMode;
  onPermissionModeChange: (mode: PermissionMode) => void;
  workingDirectory: string;
  usage: TokenUsage | null;
}) {
  const theme = useTheme();
  const [draft, setDraft] = useState("");
  // A multiline `TextInput` does not grow with its content on any platform — the web client gets
  // that from `field-sizing: content`, which has no equivalent here. Measured and clamped, so a
  // long message opens the box instead of scrolling inside a two-line window.
  const [draftHeight, setDraftHeight] = useState(theme.controlHeight);
  const [agentSheet, setAgentSheet] = useState(false);
  const [modeSheet, setModeSheet] = useState(false);
  const [agents, setAgents] = useState<AgentSummary[]>([]);

  // Keyed on `disabled` as well as the directory, because `disabled` is "the machine is not
  // reachable" — and a lookup fired before the connection settled would fail once and never be
  // retried, leaving the agent sheet permanently empty on a cold start.
  useEffect(() => {
    if (disabled) return;
    fetchAgents(workingDirectory).then(setAgents).catch(() => {});
  }, [workingDirectory, disabled]);

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    setDraftHeight(theme.controlHeight);
    onSend(text);
  }, [draft, onSend, theme.controlHeight]);

  const dictation = useDictation(!disabled, (spoken) => {
    // Appended, never substituted: somebody who typed half a sentence and then spoke the rest
    // meant both halves.
    setDraft((current) => (current.trim() ? `${current.trimEnd()} ${spoken}` : spoken));
  });

  const ModeIcon = PERMISSION_ICON[permissionMode];
  const placeholder = disabled ? "Not connected"
    : queueOnly ? "Queue a message for the next turn…"
    : streaming ? "Send a message to steer this turn…"
    : "Send a message…";

  return (
    <View style={{ gap: theme.space[2] }}>
      <View style={[styles.inputRow, { gap: theme.space[2] }]}>
        <TextInput
          value={draft}
          onChangeText={setDraft}
          placeholder={placeholder}
          placeholderTextColor={theme.colors.fgSubtle}
          editable={!disabled}
          multiline
          onContentSizeChange={({ nativeEvent }) => setDraftHeight(nativeEvent.contentSize.height)}
          style={[
            theme.text.body,
            {
              flex: 1,
              color: theme.colors.fg,
              backgroundColor: theme.colors.bgPanel,
              borderColor: theme.colors.border,
              borderWidth: 1,
              borderRadius: theme.radii.md,
              paddingHorizontal: theme.space[2.5],
              paddingTop: theme.space[2.5],
              paddingBottom: theme.space[2.5],
              height: Math.min(Math.max(draftHeight, theme.controlHeight), COMPOSER_MAXIMUM_HEIGHT),
            },
          ]}
        />
        {dictation.available ? (
          <IconButton
            icon={dictation.recording ? MicOff : Mic}
            busy={dictation.busy}
            tone={dictation.recording ? "danger" : "neutral"}
            filled={dictation.recording}
            onPress={dictation.toggle}
            disabled={disabled}
          />
        ) : null}
        {streaming ? (
          <IconButton icon={Square} tone="danger" filled onPress={onStop} />
        ) : (
          <IconButton icon={ArrowUp} tone="accent" filled onPress={send} disabled={disabled || !draft.trim()} />
        )}
      </View>

      {dictation.failure ? <Text variant="small" tone="danger">{dictation.failure}</Text> : null}

      <View style={[styles.chips, { gap: theme.space[2] }]}>
        <Chip icon={Bot} label={agent} onPress={() => setAgentSheet(true)} disabled={locked} />
        <Chip icon={ModeIcon} label={shortMode(permissionMode)} onPress={() => setModeSheet(true)} />
        <View style={{ flex: 1 }} />
        {usage && usage.contextWindow > 0 ? (
          <Pill
            label={`${Math.round((usage.cumulativeTotal / usage.contextWindow) * 100)}%`}
            tone={contextTone(usage)}
          />
        ) : null}
      </View>

      <Sheet open={agentSheet} onClose={() => setAgentSheet(false)} title="Which agent">
        {agents.map((entry) => (
          <Row
            key={entry.id}
            icon={Bot}
            title={entry.title || entry.name}
            subtitle={entry.description}
            selected={entry.name === agent}
            onPress={() => { onAgentChange(entry.name); setAgentSheet(false); }}
          />
        ))}
        {agents.length === 0 ? <Text variant="small" tone="subtle">No agents to choose from.</Text> : null}
      </Sheet>

      <Sheet open={modeSheet} onClose={() => setModeSheet(false)} title="How much to ask">
        {PERMISSION_MODES.map((entry) => (
          <Row
            key={entry.value}
            icon={PERMISSION_ICON[entry.value]}
            title={entry.label}
            subtitle={entry.description}
            selected={entry.value === permissionMode}
            onPress={() => { onPermissionModeChange(entry.value); setModeSheet(false); }}
          />
        ))}
      </Sheet>
    </View>
  );
}

function shortMode(mode: PermissionMode): string {
  return mode === "default" ? "Ask" : mode === "auto" ? "Auto" : "Read-only";
}

function contextTone(usage: TokenUsage): "neutral" | "warning" | "danger" {
  const share = usage.cumulativeTotal / usage.contextWindow;
  if (share >= 0.9) return "danger";
  if (share >= 0.75) return "warning";
  return "neutral";
}

function Chip({
  icon: Icon, label, onPress, disabled,
}: {
  icon: typeof Bot;
  label: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  const theme = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [
        styles.chip,
        {
          height: 32,
          borderRadius: theme.radii.md,
          borderColor: theme.colors.border,
          borderWidth: 1,
          backgroundColor: theme.colors.bg,
          paddingHorizontal: theme.space[2],
          gap: theme.space[1.5],
          opacity: disabled ? 0.5 : pressed ? 0.7 : 1,
        },
      ]}
    >
      <Icon size={13} color={theme.colors.fgMuted} />
      <Text variant="label" tone="muted" numberOfLines={1}>{label}</Text>
    </Pressable>
  );
}

function IconButton({
  icon: Icon, onPress, tone = "neutral", filled, disabled, busy,
}: {
  icon: typeof Mic;
  onPress: () => void;
  tone?: "neutral" | "accent" | "danger";
  filled?: boolean;
  disabled?: boolean;
  busy?: boolean;
}) {
  const theme = useTheme();
  const solid = tone === "danger" ? theme.colors.redSolid : tone === "accent" ? theme.colors.blueSolid : theme.colors.fg;
  const foreground = filled ? theme.colors.onSolid : theme.colors.fgMuted;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || busy}
      style={({ pressed }) => [
        styles.iconButton,
        {
          width: theme.controlHeight,
          height: theme.controlHeight,
          borderRadius: theme.radii.md,
          backgroundColor: filled ? solid : "transparent",
          borderColor: filled ? "transparent" : theme.colors.border,
          borderWidth: filled ? 0 : 1,
          opacity: disabled ? 0.4 : pressed ? 0.7 : 1,
        },
      ]}
    >
      {busy ? <ActivityIndicator size="small" color={foreground} /> : <Icon size={18} color={foreground} />}
    </Pressable>
  );
}

/**
 * The microphone's whole state machine.
 *
 * The status is asked for with `prepare`, which tells the daemon to start loading the model as
 * well as reporting on it — so the gigabyte of weights arrives while somebody is reading their
 * conversation rather than while they wait with a finger on the button.
 */
function useDictation(connected: boolean, onTranscribed: (text: string) => void) {
  const recorder = useAudioRecorder(DICTATION_RECORDING);
  const [status, setStatus] = useState<DictationStatus | null>(null);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const active = useRef(false);

  useEffect(() => {
    if (!connected) return;
    let stop = false;
    const read = async () => {
      try {
        const found = await fetchDictationStatus(true);
        if (!stop) setStatus(found);
        // Polled only while the model is loading, which is the one state that ends on its own.
        if (!stop && found.state === "loading") setTimeout(read, 1000);
      } catch {
        // Dictation being unreachable is the connection's problem to report, not this button's.
      }
    };
    void read();
    return () => {
      stop = true;
      if (active.current) recorder.stop().catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected]);

  const toggle = useCallback(async () => {
    setFailure("");
    if (!DICTATION_SUPPORTED) {
      setFailure(dictationUnavailableReason());
      return;
    }
    if (recording) {
      setRecording(false);
      active.current = false;
      setBusy(true);
      try {
        await recorder.stop();
        const uri = recorder.uri;
        if (!uri) throw new Error("The recording came back empty.");
        const samples = await samplesFrom(uri);
        if (samples.length === 0) throw new Error("Nothing was recorded.");
        const spoken = (await transcribeDictation(samples)).trim();
        if (spoken) onTranscribed(spoken);
      } catch (caught) {
        setFailure(caught instanceof Error ? caught.message : "That did not transcribe.");
      } finally {
        setBusy(false);
      }
      return;
    }
    if (!(await requestMicrophone())) {
      setFailure("Frank needs the microphone to hear you.");
      return;
    }
    try {
      await recorder.prepareToRecordAsync();
      recorder.record();
      active.current = true;
      setRecording(true);
      if (Platform.OS !== "web") await Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    } catch (caught) {
      setFailure(caught instanceof Error ? caught.message : "The microphone would not start.");
    }
  }, [recording, recorder, onTranscribed]);

  return {
    available: !!status?.enabled,
    recording,
    busy: busy || status?.state === "loading",
    failure: failure || (status?.state === "failed" ? status.failure : ""),
    toggle: () => { void toggle(); },
  };
}

const styles = StyleSheet.create({
  inputRow: { flexDirection: "row", alignItems: "flex-end" },
  chips: { flexDirection: "row", alignItems: "center" },
  chip: { flexDirection: "row", alignItems: "center" },
  iconButton: { alignItems: "center", justifyContent: "center" },
});
