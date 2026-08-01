/**
 * Settings, which on a phone means two different things and says so.
 *
 * The top is about *this device*: which machine it is paired with, what it is called here, and
 * how it looks. The rest is the machine's own settings, in the desktop's shape — a title and a
 * description on the left, the control on the right — reading the same strings out of
 * `shared/messages`. These were read-only `on`/`off` badges, which was two mistakes at once: a
 * badge is not a control, and `off` is a value rather than a label.
 *
 * What is still absent is deliberate. API keys, sandbox profiles and provider credentials are
 * typed on a keyboard in front of the thing they configure, and a phone is not that.
 */

import { router } from "expo-router";
import { Link2, MonitorSmartphone, Pencil, RefreshCw, Trash2, X } from "lucide-react-native";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Pressable, ScrollView, StyleSheet, Switch, TextInput, View } from "react-native";

import { labels } from "@shared/labels";

import { Button, Card, Divider, SectionLabel, Text } from "../components/ui";
import {
  fetchSettings, updateComputerControlSetting, updateDictationSetting, updateUserContextSetting,
  type Settings,
} from "../lib/api";
import { useConnection } from "../lib/connection";
import { goBack } from "../lib/navigation";
import { useTheme, type ThemePreference } from "../theme";
import { useEdgeInsets } from "../theme/insets";

const say = labels("SettingsDialog");
const control = labels("SessionControls");

const THEMES: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

/**
 * One setting: what it is on the left, the control on the right.
 *
 * The desktop's `SettingRow`, in React Native. Every setting reads the same way because every
 * setting is this — which is precisely what a scattering of little badges was not.
 */
function SettingRow({
  title, description, children,
}: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  const theme = useTheme();
  return (
    <View style={[styles.row, { paddingVertical: theme.space[3], gap: theme.space[4] }]}>
      <View style={{ flex: 1, minWidth: 0, gap: theme.space[0.5] }}>
        <Text variant="label">{title}</Text>
        {description ? <Text variant="small" tone="muted">{description}</Text> : null}
      </View>
      {children ? <View style={{ flexShrink: 0 }}>{children}</View> : null}
    </View>
  );
}

export default function SettingsScreen() {
  const theme = useTheme();
  const insets = useEdgeInsets();
  const { status, pairing, endpoint, unpair, reconnect, rename } = useConnection();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [draftName, setDraftName] = useState("");

  useEffect(() => {
    if (status !== "online") return;
    fetchSettings().then(setSettings).catch(() => {});
  }, [status]);

  const forget = useCallback(async () => {
    await unpair();
    router.replace("/pair");
  }, [unpair]);

  /** Flip a switch on screen at once, and put it back if the machine disagrees. */
  const toggle = useCallback(
    (key: keyof Settings, save: (value: boolean) => Promise<void>) => (value: boolean) => {
      setSettings((previous) => (previous ? { ...previous, [key]: value } : previous));
      save(value).catch(() => { fetchSettings().then(setSettings).catch(() => {}); });
    },
    [],
  );

  const machine = pairing?.name ?? "the machine";
  const confined = !!settings?.sandbox_backend?.backend && settings?.sandbox?.enforce !== "off";
  const sandboxState = settings?.sandbox_backend?.backend
    ? (confined ? control("sandboxRestricted") : control("sandboxGlobal"))
    : control("sandboxUnavailable");

  return (
    <View style={{ flex: 1, backgroundColor: theme.colors.bg, paddingTop: insets.top }}>
      <View style={[styles.bar, { paddingHorizontal: theme.space[4], paddingBottom: theme.space[2] }]}>
        <Text variant="heading" style={{ flex: 1 }}>{say("title")}</Text>
        <Pressable onPress={() => goBack()} hitSlop={12}>
          <X size={22} color={theme.colors.fgMuted} />
        </Pressable>
      </View>

      <ScrollView
        contentContainerStyle={{ paddingHorizontal: theme.space[3], paddingBottom: insets.bottom + theme.space[8] }}
      >
        <SectionLabel>{say("currentConnection")}</SectionLabel>
        <Card style={{ padding: theme.space[3], gap: theme.space[2.5] }}>
          {renaming ? (
            <View style={{ gap: theme.space[2] }}>
              <TextInput
                value={draftName}
                onChangeText={setDraftName}
                autoFocus
                placeholder={pairing?.name ?? ""}
                placeholderTextColor={theme.colors.fgSubtle}
                style={[
                  theme.text.body,
                  {
                    color: theme.colors.fg,
                    backgroundColor: theme.colors.bgSubtle,
                    borderColor: theme.colors.border,
                    borderWidth: 1,
                    borderRadius: theme.radii.md,
                    paddingHorizontal: theme.space[2.5],
                    height: theme.controlHeight,
                  },
                ]}
                onSubmitEditing={() => { void rename(draftName); setRenaming(false); }}
              />
              <View style={{ flexDirection: "row", gap: theme.space[2] }}>
                <Button label="Cancel" onPress={() => setRenaming(false)} style={{ flex: 1 }} />
                <Button
                  label="Save" variant="solid" tone="accent" style={{ flex: 1 }}
                  onPress={() => { void rename(draftName); setRenaming(false); }}
                />
              </View>
            </View>
          ) : (
            <Pressable
              onPress={() => { setDraftName(pairing?.name ?? ""); setRenaming(true); }}
              style={[styles.line, { gap: theme.space[2] }]}
            >
              <MonitorSmartphone size={16} color={theme.colors.fgMuted} />
              <Text variant="body" style={{ flex: 1 }} numberOfLines={1}>{pairing?.name ?? "Not paired"}</Text>
              <Pencil size={14} color={theme.colors.fgSubtle} />
            </Pressable>
          )}

          <View style={[styles.line, { gap: theme.space[2] }]}>
            <Link2 size={16} color={theme.colors.fgSubtle} />
            <Text variant="small" tone="muted" style={{ flex: 1 }} numberOfLines={1}>
              {statusSentence(status, endpoint, pairing?.endpoints.length ?? 0)}
            </Text>
          </View>

          <View style={{ flexDirection: "row", gap: theme.space[2] }}>
            <Button label="Reconnect" icon={RefreshCw} onPress={reconnect} style={{ flex: 1 }} />
            <Button label="Forget" icon={Trash2} tone="danger" onPress={() => void forget()} style={{ flex: 1 }} />
          </View>
        </Card>

        <SectionLabel>{say("appearance")}</SectionLabel>
        <Card style={{ paddingHorizontal: theme.space[3] }}>
          <SettingRow title="Theme" description="Follow the system, or pick one." />
          <View style={{ flexDirection: "row", gap: theme.space[1.5], paddingBottom: theme.space[3] }}>
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

        <SectionLabel>{say("runtime")}</SectionLabel>
        <Card style={{ paddingHorizontal: theme.space[3] }}>
          {settings === null ? (
            <SettingRow
              title={status === "online" ? "Reading…" : "Not connected"}
              description={status === "online" ? undefined : `Connect to see what ${machine} is set to.`}
            />
          ) : (
            <>
              <SettingRow
                title={say("approvalMode")}
                description={control(`permission${modeSuffix(settings.permission_mode)}Description`)}
              >
                <Text variant="label" tone="muted">
                  {control(`permission${modeSuffix(settings.permission_mode)}Label`)}
                </Text>
              </SettingRow>
              <Divider />
              <SettingRow
                title={say("dictation")}
                description={settings.dictation_enabled ? control("dictationOn") : control("dictationOff")}
              >
                <Switch
                  value={settings.dictation_enabled}
                  onValueChange={toggle("dictation_enabled", updateDictationSetting)}
                  trackColor={{ true: theme.colors.blueSolid, false: theme.colors.bgEmphasized }}
                />
              </SettingRow>
              <Divider />
              <SettingRow
                title={say("computerControl")}
                description={settings.computer_control_enabled ? control("computerControlOn") : control("computerControlOff")}
              >
                <Switch
                  value={settings.computer_control_enabled}
                  onValueChange={toggle("computer_control_enabled", updateComputerControlSetting)}
                  trackColor={{ true: theme.colors.blueSolid, false: theme.colors.bgEmphasized }}
                />
              </SettingRow>
              <Divider />
              <SettingRow
                title={say("userContext")}
                description={settings.user_context_enabled ? control("userContextOn") : control("userContextOff")}
              >
                <Switch
                  value={settings.user_context_enabled}
                  onValueChange={toggle("user_context_enabled", updateUserContextSetting)}
                  trackColor={{ true: theme.colors.blueSolid, false: theme.colors.bgEmphasized }}
                />
              </SettingRow>
              <Divider />
              <SettingRow
                title={say("filesystemProtection")}
                description={settings.sandbox_backend?.backend
                  ? settings.sandbox_backend.detail
                  : say("filesystemProtectionUnavailable")}
              >
                <Text variant="label" tone={confined ? "success" : "warning"}>{sandboxState}</Text>
              </SettingRow>
            </>
          )}
        </Card>

        <Text variant="small" tone="subtle" style={{ padding: theme.space[3] }}>
          Keys, sandbox profiles and model providers are set on {machine} itself.
        </Text>
      </ScrollView>
    </View>
  );
}

/** The catalogue spells the modes `permissionDefaultLabel`, `permissionAutoLabel`, and so on. */
function modeSuffix(mode: string): string {
  return mode === "auto" ? "Auto" : mode === "read_only" ? "ReadOnly" : "Default";
}

/** One sentence rather than a badge: what the connection is doing, and where. */
function statusSentence(status: string, endpoint: string, addresses: number): string {
  if (status === "online") return endpoint;
  if (status === "connecting") return "Looking for it…";
  if (status === "rejected") return "This phone is no longer paired.";
  if (status === "offline") {
    return addresses > 1 ? `Not answering on any of its ${addresses} addresses.` : "Not answering.";
  }
  return "No machine paired.";
}

const styles = StyleSheet.create({
  bar: { flexDirection: "row", alignItems: "center" },
  line: { flexDirection: "row", alignItems: "center" },
  row: { flexDirection: "row", alignItems: "center" },
});
