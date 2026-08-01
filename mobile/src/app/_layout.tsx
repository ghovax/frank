/**
 * The shell every screen sits in.
 *
 * Three things happen here and nowhere else: the typefaces are loaded, the theme is provided, and
 * the pairing is read off the keychain. The last of those decides what the app is: without one
 * there is nothing to show but the pairing screen, so `index` redirects rather than each screen
 * having to check.
 */

import { useFonts } from "expo-font";
import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { ConnectionProvider } from "../lib/connection";
import { ThemeProvider, useTheme } from "../theme";
import { FONT_SOURCES } from "../theme/fonts";

SplashScreen.preventAutoHideAsync().catch(() => {});

export default function RootLayout() {
  const [fontsLoaded, fontError] = useFonts(FONT_SOURCES);

  useEffect(() => {
    // Hidden on either outcome. A typeface that will not load is a worse-looking app, not a
    // reason to hold a splash screen in front of somebody forever.
    if (fontsLoaded || fontError) SplashScreen.hideAsync().catch(() => {});
  }, [fontsLoaded, fontError]);

  if (!fontsLoaded && !fontError) return null;

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider>
        <ThemeProvider>
          <ConnectionProvider>
            <Navigation />
          </ConnectionProvider>
        </ThemeProvider>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  );
}

function Navigation() {
  const theme = useTheme();
  return (
    <>
      <StatusBar style={theme.scheme === "dark" ? "light" : "dark"} />
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: theme.colors.bg },
          // There are two screens: the interface, and the one that points at it.
          animation: "slide_from_right",
        }}
      >
        <Stack.Screen name="index" />
        <Stack.Screen name="pair" options={{ presentation: "modal", animation: "slide_from_bottom" }} />
      </Stack>
    </>
  );
}
