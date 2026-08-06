/** Pairing: pointing this phone at a machine, once, by camera or by pasted link. */

import { CameraView, useCameraPermissions } from "expo-camera";
import * as Clipboard from "expo-clipboard";
import * as Haptics from "expo-haptics";
import { router, useLocalSearchParams } from "expo-router";
import { Camera, ClipboardPaste, ScanLine, X } from "lucide-react-native";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import {
  ActivityIndicator, Linking, Platform, Pressable, ScrollView, StyleSheet, TextInput, View,
} from "react-native";

import { Alert, Button, Card, Text } from "../components/ui";
import { PairingError, parsePairing, useConnection } from "../lib/connection";
import { goBack } from "../lib/navigation";
import { useTheme } from "../theme";
import { useEdgeInsets } from "../theme/insets";

export default function PairScreen() {
  const translation = useTranslations("PairScreen");
  const theme = useTheme();
  const insets = useEdgeInsets();
  const { add, machines } = useConnection();
  const { link } = useLocalSearchParams<{ link?: string }>();
  const [permission, requestPermission] = useCameraPermissions();
  // The camera is the default everywhere it exists; on the web there is nothing to point it at.
  const [mode, setMode] = useState<"scan" | "paste">(Platform.OS === "web" ? "paste" : "scan");
  const [typed, setTyped] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  // A scanner fires the same code many times a second, so all but the first are dropped.
  const claimed = useRef(false);

  const accept = useCallback(async (raw: string) => {
    if (claimed.current) return;
    let parsed;
    try {
      parsed = parsePairing(raw);
    } catch (caught) {
      // The parser says which way the code was wrong by throwing a key, having no catalogue to reach.
      setFailure(translation(caught instanceof PairingError ? caught.reason : "notAPairingCode"));
      return;
    }
    claimed.current = true;
    setBusy(true);
    setFailure("");
    if (Platform.OS !== "web") await Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
    await add(parsed);
    // Straight to the machine just paired, over the list rather than instead of it.
    router.dismissTo("/");
    router.push("/interface");
  }, [add, translation]);

  // Ask for the camera as soon as the scanner is what is on screen.
  useEffect(() => {
    if (mode !== "scan" || Platform.OS === "web") return;
    if (permission === null || permission.granted || !permission.canAskAgain) return;
    void requestPermission();
  }, [mode, permission, requestPermission]);

  // A link opened from outside arrives as a route parameter, which takes the same path as scanning.
  useEffect(() => {
    // A link that will not parse reports itself immediately rather than sitting there looking ready.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (link) void accept(String(link));
  }, [link, accept]);

  /** The clipboard, when there is one, which a plain-HTTP browser does not have at all. */
  const paste = useCallback(async () => {
    try {
      const text = await Clipboard.getStringAsync();
      if (text) {
        setTyped(text);
        setFailure("");
      }
    } catch {
      setFailure(translation("clipboardUnavailable"));
    }
  }, [translation]);

  return (
    <View style={[styles.screen, { backgroundColor: theme.colors.bg, paddingTop: insets.top + theme.space[2] }]}>
      <View style={[styles.header, { paddingHorizontal: theme.space[4], paddingBottom: theme.space[3] }]}>
        <Text variant="heading" style={{ flex: 1 }}>{translation("title")}</Text>
        {machines.length > 0 ? (
          <Pressable onPress={() => goBack()} hitSlop={12}>
            <X size={22} color={theme.colors.fgMuted} />
          </Pressable>
        ) : null}
      </View>

      <ScrollView
        contentContainerStyle={{ padding: theme.space[4], paddingTop: 0, gap: theme.space[3], paddingBottom: insets.bottom + theme.space[6] }}
        keyboardShouldPersistTaps="handled"
      >
        <Text variant="body" tone="muted">
          {translation.rich("instructions", {
            command: (parts) => <Text variant="mono">{parts}</Text>,
          })}
        </Text>

        <View style={[styles.tabs, { gap: theme.space[2] }]}>
          <Button
            label={translation("scan")} icon={ScanLine} full style={{ flex: 1 }}
            variant={mode === "scan" ? "subtle" : "outline"}
            tone={mode === "scan" ? "accent" : "neutral"}
            onPress={() => setMode("scan")}
          />
          <Button
            label={translation("pasteLink")} icon={ClipboardPaste} full style={{ flex: 1 }}
            variant={mode === "paste" ? "subtle" : "outline"}
            tone={mode === "paste" ? "accent" : "neutral"}
            onPress={() => setMode("paste")}
          />
        </View>

        {mode === "scan" ? (
          <Card style={{ overflow: "hidden", aspectRatio: 1 }}>
            {Platform.OS === "web" ? (
              <View style={styles.center}>
                <Text variant="small" tone="subtle" align="center">{translation("noCameraHere")}</Text>
              </View>
            ) : permission?.granted ? (
              <CameraView
                style={StyleSheet.absoluteFill}
                barcodeScannerSettings={{ barcodeTypes: ["qr"] }}
                onBarcodeScanned={({ data }) => void accept(data)}
              />
            ) : permission && !permission.canAskAgain ? (
              // Refused before, so the only honest instruction is where to change it, or to use the other tab.
              <View style={[styles.center, { gap: theme.space[3], padding: theme.space[4] }]}>
                <Camera size={26} color={theme.colors.fgSubtle} />
                <Text variant="small" tone="muted" align="center">{translation("cameraRefused")}</Text>
                <Button
                  label={translation("openSettings")} tone="accent" variant="solid"
                  onPress={() => void Linking.openSettings()}
                />
              </View>
            ) : (
              // Between the tab appearing and the system dialog being answered.
              <View style={[styles.center, { gap: theme.space[3], padding: theme.space[4] }]}>
                <ActivityIndicator color={theme.colors.fgMuted} />
                <Text variant="small" tone="subtle" align="center">{translation("waitingForCamera")}</Text>
              </View>
            )}
          </Card>
        ) : (
          <Card style={{ padding: theme.space[3], gap: theme.space[3] }}>
            <TextInput
              value={typed}
              onChangeText={(next) => { setTyped(next); setFailure(""); }}
              placeholder={translation("linkPlaceholder")}
              placeholderTextColor={theme.colors.fgSubtle}
              autoCapitalize="none"
              autoCorrect={false}
              multiline
              style={[
                theme.text.mono,
                {
                  color: theme.colors.fg,
                  backgroundColor: theme.colors.bgSubtle,
                  borderColor: theme.colors.border,
                  borderWidth: 1,
                  borderRadius: theme.radii.md,
                  padding: theme.space[2.5],
                  minHeight: 96,
                  textAlignVertical: "top",
                },
              ]}
            />
            <View style={{ flexDirection: "row", gap: theme.space[2] }}>
              <Button label={translation("paste")} icon={ClipboardPaste} onPress={() => void paste()} style={{ flex: 1 }} />
              <Button
                label={translation("connect")} variant="solid" tone="accent" busy={busy}
                disabled={!typed.trim()} onPress={() => void accept(typed)} style={{ flex: 1 }}
              />
            </View>
          </Card>
        )}

        {failure ? <Alert status="error">{failure}</Alert> : null}

        <Text variant="small" tone="subtle">{translation("tokenWarning")}</Text>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1 },
  header: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  tabs: { flexDirection: "row" },
  center: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center" },
});
