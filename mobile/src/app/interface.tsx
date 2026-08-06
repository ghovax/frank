/** LangMesh on a phone: the desktop interface, in a window, with no second interface here. */

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { ActivityIndicator, Platform, RefreshControl, ScrollView, StyleSheet, View } from "react-native";
import { WebView } from "react-native-webview";

import { ExternalLink, RotateCw, ScanLine } from "lucide-react-native";

import { LangMeshMark } from "../components/langmesh-mark";
import { Button, Text } from "../components/ui";
import { useConnection } from "../lib/connection";
import { goBack } from "../lib/navigation";
import { useTheme } from "../theme";
import { useEdgeInsets } from "../theme/insets";

export default function InterfaceScreen() {
  const translation = useTranslations("InterfaceScreen");
  const theme = useTheme();
  const { status, active, reconnect } = useConnection();
  const view = useRef<WebView>(null);
  const [loading, setLoading] = useState(true);

  // Back to the list when there is nothing to show, distinguishing having left from a token gone bad.
  useEffect(() => {
    if (status === "idle" || status === "rejected") goBack();
  }, [status]);

  /** The one URL this app knows, carrying the token exactly once so the rest rides a session cookie. */
  const source = active && status === "online"
    ? `${active.endpoint}/?token=${encodeURIComponent(active.token)}`
    : "";

  const reload = useCallback(() => {
    setLoading(true);
    view.current?.reload();
  }, []);

  if (status !== "online") {
    return (
      <Waiting
        status={status}
        machine={active?.name ?? translation("thisMachine")}
        onRetry={reconnect}
        endpoint={active?.endpoint ?? ""}
      />
    );
  }

  // A browser cannot be the shell, so in one it hands over rather than framing the interface.
  if (Platform.OS === "web") {
    return <HandOver machine={active?.name ?? translation("thisMachine")} url={source} />;
  }

  return (
    // No padding here on purpose, since the page reserves the notch and the home indicator itself.
    <View style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <WebView
        ref={view}
        source={{ uri: source }}
        style={{ flex: 1, backgroundColor: theme.colors.bg }}
        // Deliberately not the webview's back gesture: the edge swipe belongs to the shell now.
        mediaCapturePermissionGrantType="grant"
        allowsInlineMediaPlayback
        mediaPlaybackRequiresUserAction={false}
        // The token became a session cookie, without which the interface would ask to be paired every launch.
        sharedCookiesEnabled
        thirdPartyCookiesEnabled={false}
        onLoadEnd={() => setLoading(false)}
        onError={() => setLoading(false)}
        renderError={() => <Fallen machine={active?.name ?? translation("thisMachine")} onRetry={reload} />}
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

/** In a browser: the door, rather than the room. */
function HandOver({ machine, url }: { machine: string; url: string }) {
  const translation = useTranslations("InterfaceScreen");
  const theme = useTheme();
  const insets = useEdgeInsets();
  return (
    <View
      style={[
        styles.centre,
        {
          backgroundColor: theme.colors.bg,
          gap: theme.space[4],
          paddingTop: insets.top + theme.space[6],
          paddingBottom: insets.bottom + theme.space[6],
          paddingHorizontal: theme.space[6],
        },
      ]}
    >
      <LangMeshMark size={40} color={theme.colors.fgSubtle} />
      <Text variant="body" tone="muted" align="center">{translation("pairedWith", { machine })}</Text>
      <Button
        label={translation("openLangMesh")} icon={ExternalLink} variant="solid" tone="accent"
        // Replacing rather than opening, since a browser would treat a new window as a popup.
        onPress={() => { window.location.replace(url); }}
      />
      <Button label={translation("otherMachines")} icon={ScanLine} onPress={() => goBack()} />
    </View>
  );
}

/** Before the interface can load: what the connection is doing, and what to do about it. */
function Waiting({ status, machine, onRetry, endpoint }: {
  status: string;
  machine: string;
  onRetry: () => void;
  endpoint: string;
}) {
  const translation = useTranslations("InterfaceScreen");
  const theme = useTheme();
  const insets = useEdgeInsets();
  const [refreshing, setRefreshing] = useState(false);

  const message = status === "connecting"
    ? translation("lookingFor", { machine })
    : translation("notAnswering", { machine });


  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: theme.colors.bg }}
      contentContainerStyle={[
        styles.centre,
        {
          // Longhand, and the order matters, because a shorthand written after would override the inset.
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
      <LangMeshMark size={40} color={theme.colors.fgSubtle} />
      <Text variant="body" tone="muted" align="center">{message}</Text>
      {status === "connecting" ? (
        <ActivityIndicator color={theme.colors.fgMuted} />
      ) : (
        <>
          <Button label={translation("tryAgain")} icon={RotateCw} onPress={onRetry} />
          {/* The addresses being tried, and the way out, so "not answering" is something a person can check. */}
          {endpoint ? (
            <Text variant="small" tone="subtle" align="center">
              {translation("tried", { address: endpoint })}
            </Text>
          ) : null}
        </>
      )}
    </ScrollView>
  );
}

/** The interface itself failed to load, which is a different problem from not being reachable. */
function Fallen({ machine, onRetry }: { machine: string; onRetry: () => void }) {
  const translation = useTranslations("InterfaceScreen");
  const theme = useTheme();
  return (
    <View style={[styles.centre, { backgroundColor: theme.colors.bg, gap: theme.space[4], padding: theme.space[6] }]}>
      <Text variant="title" align="center">{translation("wouldNotLoad")}</Text>
      <Text variant="small" tone="muted" align="center">{translation("wouldNotLoadBody", { machine })}</Text>
      <Button label={translation("tryAgain")} icon={RotateCw} onPress={onRetry} />
    </View>
  );
}

const styles = StyleSheet.create({
  centre: { flexGrow: 1, alignItems: "center", justifyContent: "center" },
  veil: { position: "absolute", top: 0, left: 0, right: 0, bottom: 0, alignItems: "center", justifyContent: "center" },
});
