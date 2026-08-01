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
 * What stays native is only what a page cannot do: reading a pairing code with the camera, and
 * keeping the token in the keychain.
 *
 * Dictation was briefly on that list and is not. Over plain HTTP a webview is not a secure
 * context, so the microphone was closed to the page and the shell recorded on its behalf — a
 * second recording implementation, in a second language, of something the desktop already did.
 * Reaching the machine over HTTPS makes the page a secure context and the whole apparatus
 * unnecessary, which is the better fix: the phone dictates with the desktop's own code.
 */

import { router } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { ActivityIndicator, Platform, RefreshControl, ScrollView, StyleSheet, View } from "react-native";
import { WebView } from "react-native-webview";

import { ExternalLink, RotateCw, ScanLine } from "lucide-react-native";

import { FrankMark } from "../components/frank-mark";
import { Button, Text } from "../components/ui";
import { useConnection } from "../lib/connection";
import { useTheme } from "../theme";
import { useEdgeInsets } from "../theme/insets";

export default function InterfaceScreen() {
  const translation = useTranslations("InterfaceScreen");
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
    return (
      <Waiting
        status={status}
        machine={pairing?.name ?? translation("thisMachine")}
        onRetry={reconnect}
        endpoint={pairing?.endpoint ?? ""}
      />
    );
  }

  // A browser cannot be the shell, so in one it hands over instead.
  //
  // `WebView` on web is an `<iframe>`, and the interface inside one would be a third party to the
  // page framing it: reach's session cookie is `SameSite=Lax` and would never be sent, and every
  // current browser blocks third-party cookies outright anyway. `SameSite=None` is not a way out
  // — it requires `Secure`, and this is plain HTTP by design. So the frame would load and then
  // fail to authenticate a single thing it asked for.
  //
  // Navigating there at the top level has none of that: the interface becomes the page, first
  // party to itself, and the cookie exchange works exactly as it does in the app. Which is the
  // honest shape of it — in a browser this shell has no camera, no keychain and no microphone to
  // lend, so the one useful thing it still holds is the address and the token.
  if (Platform.OS === "web") {
    return <HandOver machine={pairing?.name ?? translation("thisMachine")} url={source} />;
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
        // Dictation is `getUserMedia` in the page, as on the desktop. `grant` answers it with
        // the microphone permission the app already holds, so the person is asked once by the
        // operating system rather than twice.
        mediaCapturePermissionGrantType="grant"
        allowsInlineMediaPlayback
        mediaPlaybackRequiresUserAction={false}
        // The token became a session cookie; without this it is dropped between loads and the
        // interface would ask to be paired again on every launch.
        sharedCookiesEnabled
        thirdPartyCookiesEnabled={false}
        onLoadEnd={() => setLoading(false)}
        onError={() => setLoading(false)}
        renderError={() => <Fallen machine={pairing?.name ?? translation("thisMachine")} onRetry={reload} />}
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
      <FrankMark size={40} color={theme.colors.fgSubtle} />
      <Text variant="body" tone="muted" align="center">{translation("pairedWith", { machine })}</Text>
      <Button
        label={translation("openFrank")} icon={ExternalLink} variant="solid" tone="accent"
        // Replacing rather than opening: a browser would treat a new window as a popup, and
        // there is nothing on this screen worth going back to.
        onPress={() => { window.location.replace(url); }}
      />
      <Button label={translation("pairAgain")} icon={ScanLine} onPress={() => router.push("/pair")} />
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
      {status === "connecting" ? (
        <ActivityIndicator color={theme.colors.fgMuted} />
      ) : (
        <>
          <Button label={translation("tryAgain")} icon={RotateCw} onPress={onRetry} />
          {/*
            The addresses actually being tried, and the way out when none of them are right.

            The address itself is stable — a tailnet name outlives the machine's leases — so this
            is a machine that really is asleep, off the tailnet, or not running `frank reach`.
            Showing the address anyway is what turns "not answering" from a verdict into
            something a person can check.

            Showing what is being tried turns that into something a person can recognise on
            sight, and pairing again is the only thing that fixes it — which until now was
            unreachable from here, because this screen is what a phone with a stale address is
            stuck on and nothing on it led anywhere.
          */}
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
