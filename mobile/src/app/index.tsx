/**
 * Frank, on a phone: the desktop interface, in a window.
 *
 * There is no second interface here and there is not meant to be. Everything a person sees — the
 * sessions list, the transcript, the tool rows and their shimmer, the composer, the approval
 * cards, the settings — is `web/`, the same bundle the Tauri app puts in a window and the same
 * one `frank serve` hands a browser. The desktop app is already a webview around it; this is that
 * arrangement, on a device that happens to be a phone.
 *
 * A React Native port of those screens is what this replaced, and the reason is worth keeping: a
 * port can be faithful on the day it is written and cannot *stay* faithful, because nothing
 * structurally stops it drifting. It drifted — a thinking row the desktop does not have, a
 * spinner where the desktop shimmers, a workspace called `name +1`. None of that is reachable
 * from here, because there is nowhere for it to live.
 *
 * The consequence, and it is not a small one: making the interface work on a phone is now work on
 * `web/` itself. That is the right place for it — a dialog that is unusable at 390pt is unusable
 * in a narrow window on a laptop too.
 *
 * What stays native is only what a page cannot do: reading a pairing code with the camera,
 * keeping the token in the keychain, and finding which of a machine's addresses answers today.
 */

import { router } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Platform, RefreshControl, ScrollView, StyleSheet, View } from "react-native";
import { WebView } from "react-native-webview";

import { FrankMark } from "../components/frank-mark";
import { Button, Text } from "../components/ui";
import { useConnection } from "../lib/connection";
import { useTheme } from "../theme";
import { useEdgeInsets } from "../theme/insets";

export default function InterfaceScreen() {
  const theme = useTheme();
  const { status, pairing, endpoint, reconnect } = useConnection();
  const view = useRef<WebView>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (status === "unpaired" || status === "rejected") router.replace("/pair");
  }, [status]);

  /**
   * The one URL this app knows.
   *
   * The token goes in the query exactly once, on the document request: `frank reach` answers it
   * with an `HttpOnly` session cookie, and every script, font, event stream and websocket the
   * page asks for afterwards carries the token without the page ever holding it. See
   * `require_token` in `src/frank/cli/commands/reach.py`.
   */
  const source = endpoint && pairing
    ? `${endpoint}/?token=${encodeURIComponent(pairing.token)}`
    : "";

  const reload = useCallback(() => {
    setLoading(true);
    view.current?.reload();
  }, []);

  if (status !== "online") {
    return <Waiting status={status} machine={pairing?.name ?? "your Mac"} onRetry={reconnect} />;
  }

  return (
    // No padding here on purpose. The page is served `viewport-fit=cover` and reserves the
    // notch and the home indicator itself, in `globals.css`, so a shell that also inset the
    // webview would reserve them twice — a black band at the top and a gap at the bottom.
    <View style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <WebView
        ref={view}
        source={{ uri: source }}
        style={{ flex: 1, backgroundColor: theme.colors.bg }}
        // The interface is a single-page application that manages its own history, so the back
        // gesture should move within it rather than unload it.
        allowsBackForwardNavigationGestures
        // Dictation records through `getUserMedia`, which a webview will not grant unasked.
        // `grant` answers with the permission the app already holds, so the person is asked once
        // by the operating system rather than twice.
        mediaCapturePermissionGrantType="grant"
        allowsInlineMediaPlayback
        mediaPlaybackRequiresUserAction={false}
        // The token became a session cookie; without this it is dropped between loads and the
        // interface would ask to be paired again on every launch.
        sharedCookiesEnabled
        thirdPartyCookiesEnabled={false}
        onLoadEnd={() => setLoading(false)}
        onError={() => setLoading(false)}
        renderError={() => <Fallen machine={pairing?.name ?? "your Mac"} onRetry={reload} />}
        automaticallyAdjustContentInsets={false}
        contentInsetAdjustmentBehavior="never"
        {...(Platform.OS === "android" ? { setSupportMultipleWindows: false } : {})}
      />
      {loading ? (
        <View style={[styles.veil, { backgroundColor: theme.colors.bg }]}>
          <ActivityIndicator color={theme.colors.fgMuted} />
        </View>
      ) : null}
    </View>
  );
}

/** Before the interface can load: what the connection is doing, and what to do about it. */
function Waiting({ status, machine, onRetry }: { status: string; machine: string; onRetry: () => void }) {
  const theme = useTheme();
  const insets = useEdgeInsets();
  const [refreshing, setRefreshing] = useState(false);

  const message = status === "connecting"
    ? `Looking for ${machine}…`
    : `${machine} is not answering. It may be asleep, or off this network.`;

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: theme.colors.bg }}
      contentContainerStyle={[
        styles.centre,
        {
          // Longhand, and the order matters: a `padding` written after `paddingTop` overrides
          // it, which had been quietly throwing the safe-area reservation away.
          paddingTop: insets.top + theme.space[6],
          paddingBottom: insets.bottom + theme.space[6],
          paddingHorizontal: theme.space[6],
          gap: theme.space[4],
        },
      ]}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          tintColor={theme.colors.fgMuted}
          onRefresh={() => {
            setRefreshing(true);
            onRetry();
            setTimeout(() => setRefreshing(false), 1200);
          }}
        />
      }
    >
      <FrankMark size={40} color={theme.colors.fgSubtle} />
      <Text variant="body" tone="muted" align="center">{message}</Text>
      {status === "connecting"
        ? <ActivityIndicator color={theme.colors.fgMuted} />
        : <Button label="Try again" onPress={onRetry} />}
    </ScrollView>
  );
}

/** The interface itself failed to load, which is a different problem from not being reachable. */
function Fallen({ machine, onRetry }: { machine: string; onRetry: () => void }) {
  const theme = useTheme();
  return (
    <View style={[styles.centre, { backgroundColor: theme.colors.bg, gap: theme.space[4], padding: theme.space[6] }]}>
      <Text variant="title" align="center">The interface would not load</Text>
      <Text variant="small" tone="muted" align="center">
        {machine} answered, but did not serve the screens — it may be running a build that was
        never made.
      </Text>
      <Button label="Try again" onPress={onRetry} />
    </View>
  );
}

const styles = StyleSheet.create({
  centre: { flexGrow: 1, alignItems: "center", justifyContent: "center" },
  veil: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center" },
});
