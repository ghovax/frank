/** The shell every screen sits in. */

import { useFonts } from "expo-font";
import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { ConnectionProvider } from "../lib/connection";
import { Translations } from "../lib/intl";
import { ThemeProvider, useTheme } from "../theme";
import { FONT_SOURCES } from "../theme/fonts";

SplashScreen.preventAutoHideAsync().catch(() => {});

export default function RootLayout() {
  const [fontsLoaded, fontError] = useFonts(FONT_SOURCES);

  useEffect(() => {
    // Hidden on either outcome.
    if (fontsLoaded || fontError) SplashScreen.hideAsync().catch(() => {});
  }, [fontsLoaded, fontError]);

  if (!fontsLoaded && !fontError) return null;

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider>
        <Translations>
          <ThemeProvider>
            <ConnectionProvider>
              <Navigation />
            </ConnectionProvider>
          </ThemeProvider>
        </Translations>
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
        {/* The machines list is the root; the interface is pushed onto it, so the edge swipe
            goes back to the list. Pairing is a modal over whichever of those is showing. */}
        <Stack.Screen name="index" />
        <Stack.Screen name="interface" />
        <Stack.Screen name="pair" options={{ presentation: "modal", animation: "slide_from_bottom" }} />
      </Stack>
    </>
  );
}
