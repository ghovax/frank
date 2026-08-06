/** The desktop app's design tokens, as values React Native can use. */

import type { ViewStyle } from "react-native";

export type ColorScheme = "light" | "dark";

/** The raw ramps. Named only so the semantic table below can refer to them. */
const gray = {
  50: "#fafafa", 100: "#f4f4f5", 200: "#e4e4e7", 300: "#d4d4d8", 400: "#a1a1aa",
  500: "#71717a", 600: "#52525b", 700: "#3f3f46", 800: "#27272a", 900: "#18181b", 950: "#111111",
};

export interface Palette {
  bg: string;
  bgSubtle: string;
  bgMuted: string;
  bgEmphasized: string;
  bgPanel: string;
  fg: string;
  fgMuted: string;
  fgSubtle: string;
  border: string;
  borderMuted: string;
  borderEmphasized: string;
  blueFg: string;
  blueSubtle: string;
  blueMuted: string;
  blueSolid: string;
  redFg: string;
  redSubtle: string;
  redMuted: string;
  redSolid: string;
  greenFg: string;
  greenSubtle: string;
  greenSolid: string;
  orangeFg: string;
  orangeSubtle: string;
  orangeSolid: string;
  yellowFg: string;
  yellowSolid: string;
  purpleFg: string;
  pinkFg: string;
  /** What sits on a solid accent — always the same, in both schemes. */
  onSolid: string;
}

export const PALETTES: Record<ColorScheme, Palette> = {
  light: {
    bg: "#ffffff",
    bgSubtle: gray[50],
    bgMuted: gray[100],
    bgEmphasized: gray[200],
    bgPanel: "#ffffff",
    fg: "#000000",
    fgMuted: gray[600],
    fgSubtle: gray[400],
    border: gray[200],
    borderMuted: gray[100],
    borderEmphasized: gray[300],
    blueFg: "#173da6",
    blueSubtle: "#dbeafe",
    blueMuted: "#bfdbfe",
    blueSolid: "#2563eb",
    redFg: "#991919",
    redSubtle: "#fee2e2",
    redMuted: "#fecaca",
    redSolid: "#dc2626",
    greenFg: "#116932",
    greenSubtle: "#dcfce7",
    greenSolid: "#16a34a",
    orangeFg: "#92310a",
    orangeSubtle: "#ffedd5",
    orangeSolid: "#ea580c",
    yellowFg: "#854d0e",
    yellowSolid: "#fcd34d",
    purpleFg: "#6b21a8",
    pinkFg: "#9d174d",
    onSolid: "#ffffff",
  },
  dark: {
    bg: "#000000",
    bgSubtle: gray[950],
    bgMuted: gray[900],
    bgEmphasized: gray[800],
    bgPanel: gray[950],
    fg: gray[50],
    fgMuted: gray[400],
    fgSubtle: gray[500],
    border: gray[800],
    borderMuted: gray[900],
    borderEmphasized: gray[700],
    blueFg: "#a3cfff",
    blueSubtle: "#14204a",
    blueMuted: "#1a3478",
    blueSolid: "#2563eb",
    redFg: "#fca5a5",
    redSubtle: "#300c0c",
    redMuted: "#511111",
    redSolid: "#dc2626",
    greenFg: "#86efac",
    greenSubtle: "#042713",
    greenSolid: "#16a34a",
    orangeFg: "#fdba74",
    orangeSubtle: "#3b1106",
    orangeSolid: "#ea580c",
    yellowFg: "#fcd34d",
    yellowSolid: "#fcd34d",
    purpleFg: "#d8b4fe",
    pinkFg: "#f9a8d4",
    onSolid: "#ffffff",
  },
};

/** Chakra's 4px spacing step, by the same keys the desktop components use. */
export const space = {
  0.5: 2, 1: 4, 1.5: 6, 2: 8, 2.5: 10, 3: 12, 3.5: 14, 4: 16, 5: 20, 6: 24, 8: 32, 10: 40, 12: 48,
} as const;

/** `md` is the universal corner on the desktop, and stays so here. */
export const radii = { xs: 2, sm: 4, md: 6, lg: 8, xl: 12, full: 9999 } as const;

export const fontSizes = {
  "2xs": 10, xs: 12, sm: 14, md: 16, lg: 18, xl: 20, "2xl": 24, "3xl": 30,
} as const;

/** The house control height: 32 on the desktop, 40 on a phone, which is the floor for a thumb. */
export const controlHeight = 40;

/** The three families, by the names `expo-font` will register them under. */
export const fonts = {
  sans: "LangMeshSans",
  sansMedium: "LangMeshSans-Medium",
  sansSemibold: "LangMeshSans-Semibold",
  sansBold: "LangMeshSans-Bold",
  display: "LangMeshDisplay-Bold",
  mono: "LangMeshMono",
} as const;

/** The desktop's text styles, which are the only typographic decisions a component should make. */
export const textStyles = {
  fieldLabel: { fontSize: fontSizes.xs, fontFamily: fonts.sansMedium },
  sectionLabel: { fontSize: fontSizes.xs, fontFamily: fonts.sansSemibold },
  panelTitle: { fontSize: fontSizes.sm, fontFamily: fonts.sansSemibold },
  body: { fontSize: fontSizes.sm, fontFamily: fonts.sans, lineHeight: 22 },
  small: { fontSize: fontSizes.xs, fontFamily: fonts.sans },
  mono: { fontSize: fontSizes.xs, fontFamily: fonts.mono },
} as const;

/** The panel shadow, ring-free so it does not double the card's own border. */
export const panelShadow: Record<ColorScheme, ViewStyle> = {
  light: {
    shadowColor: "#000000", shadowOpacity: 0.08, shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 }, elevation: 3,
  },
  dark: {
    shadowColor: "#000000", shadowOpacity: 0.45, shadowRadius: 18,
    shadowOffset: { width: 0, height: 6 }, elevation: 6,
  },
};

/** What a turn's state is called in colour, copied so a running turn is the same blue in both clients. */
export const STATUS_PALETTE: Record<string, keyof Palette> = {
  running: "blueSolid",
  completed: "fgSubtle",
  failed: "redSolid",
  input_required: "yellowSolid",
  canceled: "fgSubtle",
  background: "purpleFg",
  blocked: "yellowSolid",
  pending: "fgSubtle",
  unknown: "fgSubtle",
};
