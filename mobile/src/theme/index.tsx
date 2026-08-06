/** Reading the theme and choosing it: light, dark, or follow the system, persisted across launches. */

import AsyncStorage from "@react-native-async-storage/async-storage";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useColorScheme, type ViewStyle } from "react-native";

import {
  PALETTES, controlHeight, fontSizes, fonts, panelShadow, radii, space, textStyles,
  type ColorScheme, type Palette,
} from "./tokens";

export * from "./tokens";

export type ThemePreference = ColorScheme | "system";

const STORAGE_KEY = "langmesh:colorScheme";

interface ThemeValue {
  scheme: ColorScheme;
  preference: ThemePreference;
  setPreference: (preference: ThemePreference) => void;
  colors: Palette;
  space: typeof space;
  radii: typeof radii;
  fontSizes: typeof fontSizes;
  fonts: typeof fonts;
  text: typeof textStyles;
  controlHeight: number;
  shadow: ViewStyle;
}

const ThemeContext = createContext<ThemeValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const system = useColorScheme();
  const [preference, setStoredPreference] = useState<ThemePreference>("system");

  useEffect(() => {
    // Read once at mount; a miss means the default the state already holds.
    AsyncStorage.getItem(STORAGE_KEY)
      .then((stored) => {
        if (stored === "light" || stored === "dark" || stored === "system") setStoredPreference(stored);
      })
      .catch(() => {});
  }, []);

  const setPreference = useCallback((next: ThemePreference) => {
    setStoredPreference(next);
    AsyncStorage.setItem(STORAGE_KEY, next).catch(() => {});
  }, []);

  const scheme: ColorScheme = preference === "system" ? (system === "dark" ? "dark" : "light") : preference;

  const value = useMemo<ThemeValue>(() => ({
    scheme,
    preference,
    setPreference,
    colors: PALETTES[scheme],
    space,
    radii,
    fontSizes,
    fonts,
    text: textStyles,
    controlHeight,
    shadow: panelShadow[scheme],
  }), [scheme, preference, setPreference]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeValue {
  const value = useContext(ThemeContext);
  if (value === null) throw new Error("useTheme was called outside ThemeProvider.");
  return value;
}
