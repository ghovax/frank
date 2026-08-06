/** The shell every screen sits in: typefaces, catalogue, theme, and the pairing read off the keychain. */

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
    // Hidden on either outcome: a typeface that will not load is not a reason to hold the splash forever.
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
        {/* The machines list is the root and the interface is pushed onto it, with pairing as a modal over either. */}
        <Stack.Screen name="index" />
        <Stack.Screen name="interface" />
        <Stack.Screen name="pair" options={{ presentation: "modal", animation: "slide_from_bottom" }} />
      </Stack>
    </>
  );
}
