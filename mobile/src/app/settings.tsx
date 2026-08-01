/**
 * Settings, which on a phone means two different things and says so.
 *
 * The top half is about *this device*: which machine it is paired with, whether it can reach it,
 * and how it looks. The bottom half is about the machine, read from it and shown rather than
 * edited — API keys, sandbox profiles and provider credentials are typed on a keyboard in front
 * of the thing they configure, and a phone is not that. What the phone can usefully do is tell
 * you what is on, which is what a person away from their desk actually wants to know.
 */

import { router } from "expo-router";
import {
  Contrast, Eye, Link2, Mic, Monitor, MonitorSmartphone, RefreshCw, ShieldCheck, Trash2, X,
} from "lucide-react-native";
import { useCallback, useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Button, Card, Divider, Pill, Row, SectionLabel, Text } from "../components/ui";
import { fetchSettings, type Settings } from "../lib/api";
import { goBack } from "../lib/navigation";
import { useConnection } from "../lib/connection";
import { useTheme, type ThemePreference } from "../theme";

const THEMES: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

export default function SettingsScreen() {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  const { status, pairing, endpoint, unpair, reconnect } = useConnection();
  const [settings, setSettings] = useState<Settings | null>(null);

  useEffect(() => {
    if (status !== "online") return;
    fetchSettings().then(setSettings).catch(() => {});
  }, [status]);

  const forget = useCallback(async () => {
    await unpair();
    router.replace("/pair");
  }, [unpair]);

  return (
    <View style={{ flex: 1, backgroundColor: theme.colors.bg, paddingTop: insets.top + theme.space[2] }}>
      <View style={[styles.bar, { paddingHorizontal: theme.space[4], paddingBottom: theme.space[2] }]}>
        <Text variant="heading" style={{ flex: 1 }}>Settings</Text>
        <Pressable onPress={() => goBack()} hitSlop={12}>
          <X size={22} color={theme.colors.fgMuted} />
        </Pressable>
      </View>

      <ScrollView
        contentContainerStyle={{ paddingHorizontal: theme.space[3], paddingBottom: insets.bottom + theme.space[8] }}
      >
        <SectionLabel>This phone</SectionLabel>
        <Card style={{ padding: theme.space[3], gap: theme.space[2.5] }}>
          <View style={[styles.line, { gap: theme.space[2] }]}>
            <MonitorSmartphone size={16} color={theme.colors.fgMuted} />
            <Text variant="body" style={{ flex: 1 }}>{pairing?.name ?? "Not paired"}</Text>
            <Pill
              label={statusLabel(status)}
              tone={status === "online" ? "success" : status === "rejected" ? "danger" : "neutral"}
            />
          </View>
          {endpoint ? (
            <View style={[styles.line, { gap: theme.space[2] }]}>
              <Link2 size={16} color={theme.colors.fgSubtle} />
              <Text variant="mono" tone="subtle" numberOfLines={1} style={{ flex: 1 }}>{endpoint}</Text>
            </View>
          ) : null}
          {pairing && pairing.endpoints.length > 1 ? (
            <Text variant="small" tone="subtle">
              {pairing.endpoints.length} addresses known. Frank tries them all and keeps whichever answers.
            </Text>
          ) : null}
          <View style={{ flexDirection: "row", gap: theme.space[2] }}>
            <Button label="Reconnect" icon={RefreshCw} onPress={reconnect} style={{ flex: 1 }} />
            <Button label="Forget" icon={Trash2} tone="danger" onPress={() => void forget()} style={{ flex: 1 }} />
          </View>
        </Card>

        <SectionLabel>Appearance</SectionLabel>
        <Card style={{ padding: theme.space[2] }}>
          <View style={[styles.line, { gap: theme.space[2], paddingHorizontal: theme.space[1], paddingBottom: theme.space[2] }]}>
            <Contrast size={16} color={theme.colors.fgMuted} />
            <Text variant="body" style={{ flex: 1 }}>Theme</Text>
          </View>
          <View style={{ flexDirection: "row", gap: theme.space[1.5] }}>
            {THEMES.map((entry) => (
              <Button
                key={entry.value}
                label={entry.label}
                variant={theme.preference === entry.value ? "subtle" : "outline"}
                tone={theme.preference === entry.value ? "accent" : "neutral"}
                onPress={() => theme.setPreference(entry.value)}
                style={{ flex: 1 }}
              />
            ))}
          </View>
        </Card>

        <SectionLabel>On {pairing?.name ?? "the machine"}</SectionLabel>
        <Card>
          {settings === null ? (
            <View style={{ padding: theme.space[4] }}>
              <Text variant="small" tone="subtle">
                {status === "online" ? "Reading…" : "Connect to see what this machine is set to."}
              </Text>
            </View>
          ) : (
            <View style={{ padding: theme.space[1] }}>
              <Row icon={ShieldCheck} title="Approval mode" trailing={<Pill label={modeLabel(settings.permission_mode)} />} />
              <Divider />
              <Row
                icon={Mic}
                title="Dictation"
                subtitle={settings.dictation_enabled ? "Speech is transcribed on that Mac" : undefined}
                trailing={<Pill label={settings.dictation_enabled ? "on" : "off"} tone={settings.dictation_enabled ? "success" : "neutral"} />}
              />
              <Divider />
              <Row
                icon={Monitor}
                title="Computer control"
                trailing={<Pill label={settings.computer_control_enabled ? "on" : "off"} tone={settings.computer_control_enabled ? "warning" : "neutral"} />}
              />
              <Divider />
              <Row
                icon={Eye}
                title="Filesystem confinement"
                subtitle={settings.sandbox_backend?.detail || "nothing on that machine can enforce it"}
                trailing={<Pill label={confinement(settings).label} tone={confinement(settings).tone} />}
              />
            </View>
          )}
        </Card>
        <Text variant="small" tone="subtle" style={{ padding: theme.space[3] }}>
          These are read from the machine. Change them there — a key or a sandbox profile belongs
          in front of the thing it configures.
        </Text>
      </ScrollView>
    </View>
  );
}

function statusLabel(status: string): string {
  return status === "online" ? "connected"
    : status === "connecting" ? "looking…"
    : status === "rejected" ? "unpaired"
    : status === "offline" ? "not answering" : "no machine";
}

/**
 * Three states, not two. "Off" is a choice; "unenforceable" is the host being unable to honour
 * the choice, and the two want different colours — one is a setting, the other is a warning.
 */
function confinement(settings: Settings): { label: string; tone: "success" | "warning" | "neutral" } {
  if (!settings.sandbox_backend?.backend) return { label: "unenforceable", tone: "warning" };
  if (settings.sandbox?.enforce === "off") return { label: "off", tone: "neutral" };
  return { label: "enforced", tone: "success" };
}

function modeLabel(mode: string): string {
  return mode === "auto" ? "approve for me" : mode === "read_only" ? "read-only" : "ask first";
}

const styles = StyleSheet.create({
  bar: { flexDirection: "row", alignItems: "center" },
  line: { flexDirection: "row", alignItems: "center" },
});
