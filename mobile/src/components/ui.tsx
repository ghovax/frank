/** The primitives every screen is built from, since React Native has no cascade to resolve tokens through. */

import { CircleCheck, Info, TriangleAlert, type LucideIcon } from "lucide-react-native";
import type { ReactNode } from "react";
import {
  ActivityIndicator, Pressable, StyleSheet, Text as RNText, View,
  type StyleProp, type TextStyle, type ViewStyle,
} from "react-native";

import { useTheme } from "../theme";

type Tone = "default" | "muted" | "subtle" | "accent" | "danger" | "success" | "warning";
type Variant = "body" | "small" | "label" | "sectionLabel" | "title" | "heading" | "mono";

export function Text({
  children, variant = "body", tone = "default", style, numberOfLines, align,
}: {
  children: ReactNode;
  variant?: Variant;
  tone?: Tone;
  style?: StyleProp<TextStyle>;
  numberOfLines?: number;
  align?: "left" | "center" | "right";
}) {
  const theme = useTheme();
  const colors: Record<Tone, string> = {
    default: theme.colors.fg,
    muted: theme.colors.fgMuted,
    subtle: theme.colors.fgSubtle,
    accent: theme.colors.blueFg,
    danger: theme.colors.redFg,
    success: theme.colors.greenFg,
    warning: theme.colors.orangeFg,
  };
  const variants: Record<Variant, TextStyle> = {
    body: theme.text.body,
    small: theme.text.small,
    label: theme.text.fieldLabel,
    sectionLabel: theme.text.sectionLabel,
    title: theme.text.panelTitle,
    heading: { fontSize: theme.fontSizes["2xl"], fontFamily: theme.fonts.display },
    mono: theme.text.mono,
  };
  return (
    <RNText
      numberOfLines={numberOfLines}
      style={[variants[variant], { color: colors[tone] }, align ? { textAlign: align } : null, style]}
    >
      {children}
    </RNText>
  );
}

export function Button({
  label, onPress, icon: Icon, variant = "outline", tone = "neutral", disabled, busy, style, full,
}: {
  label?: string;
  onPress: () => void;
  icon?: LucideIcon;
  variant?: "solid" | "outline" | "subtle" | "ghost";
  tone?: "neutral" | "accent" | "danger";
  disabled?: boolean;
  busy?: boolean;
  style?: StyleProp<ViewStyle>;
  full?: boolean;
}) {
  const theme = useTheme();
  const accent = tone === "danger" ? theme.colors.redSolid : tone === "accent" ? theme.colors.blueSolid : theme.colors.fg;
  const subtleBackground = tone === "danger" ? theme.colors.redSubtle
    : tone === "accent" ? theme.colors.blueSubtle : theme.colors.bgMuted;
  const foreground = variant === "solid" ? theme.colors.onSolid
    : tone === "danger" ? theme.colors.redFg
    : tone === "accent" ? theme.colors.blueFg : theme.colors.fg;

  const background = variant === "solid" ? accent : variant === "subtle" ? subtleBackground : "transparent";
  // Every variant carries a border and only its colour changes, so selecting one never moves it.
  const border = variant === "outline" ? theme.colors.border
    : variant === "solid" ? accent
    : variant === "subtle" ? subtleBackground
    : "transparent";
  const iconSize = 16;

  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || busy}
      style={({ pressed }) => [
        styles.button,
        {
          minHeight: theme.controlHeight,
          borderRadius: theme.radii.md,
          backgroundColor: background,
          borderColor: border,
          borderWidth: 1,
          paddingHorizontal: label ? theme.space[3] : theme.space[2.5],
          opacity: disabled ? 0.45 : pressed ? 0.7 : 1,
        },
        full ? { alignSelf: "stretch" } : null,
        style,
      ]}
    >
      {/* A busy button is a centred spinner alone, pinned to the icon's size so the button does not resize. */}
      {busy ? (
        <View style={[styles.busy, { height: iconSize }]}>
          <ActivityIndicator size="small" color={foreground} />
        </View>
      ) : (
        <>
          {Icon ? <Icon size={iconSize} color={foreground} /> : null}
          {label ? (
            <RNText style={[theme.text.fieldLabel, { color: foreground, fontSize: theme.fontSizes.sm }]}>{label}</RNText>
          ) : null}
        </>
      )}
    </Pressable>
  );
}

/** The one badge. `subtle` fill, `sm` corner, small caps-free label — as on the desktop. */
export function Pill({ label, tone = "neutral", icon: Icon }: {
  label: string;
  tone?: "neutral" | "accent" | "danger" | "success" | "warning";
  icon?: LucideIcon;
}) {
  const theme = useTheme();
  const map = {
    neutral: [theme.colors.bgMuted, theme.colors.fgMuted],
    accent: [theme.colors.blueSubtle, theme.colors.blueFg],
    danger: [theme.colors.redSubtle, theme.colors.redFg],
    success: [theme.colors.greenSubtle, theme.colors.greenFg],
    warning: [theme.colors.orangeSubtle, theme.colors.orangeFg],
  } as const;
  const [background, foreground] = map[tone];
  return (
    <View style={[styles.pill, { backgroundColor: background, borderRadius: theme.radii.sm, gap: theme.space[1] }]}>
      {Icon ? <Icon size={11} color={foreground} /> : null}
      <RNText style={[theme.text.small, { color: foreground, fontSize: theme.fontSizes["2xs"] }]}>{label}</RNText>
    </View>
  );
}

/** Something the person needs to read before carrying on, in the same shape the desktop uses. */
export function Alert({ status = "error", children }: {
  status?: "info" | "warning" | "success" | "error";
  children: ReactNode;
}) {
  const theme = useTheme();
  const palette = {
    info: [theme.colors.blueSubtle, theme.colors.blueMuted, theme.colors.blueFg, Info],
    warning: [theme.colors.orangeSubtle, theme.colors.orangeSubtle, theme.colors.orangeFg, TriangleAlert],
    success: [theme.colors.greenSubtle, theme.colors.greenSubtle, theme.colors.greenFg, CircleCheck],
    error: [theme.colors.redSubtle, theme.colors.redMuted, theme.colors.redFg, TriangleAlert],
  } as const;
  const [background, border, foreground, Icon] = palette[status];
  return (
    <View
      style={[
        styles.alert,
        {
          backgroundColor: background,
          borderColor: border,
          borderRadius: theme.radii.md,
          padding: theme.space[3],
          gap: theme.space[2],
        },
      ]}
    >
      <Icon size={16} color={foreground} style={{ marginTop: 1 }} />
      <Text variant="small" style={{ color: foreground, flex: 1 }}>{children}</Text>
    </View>
  );
}

/** A floating surface: `bg.panel`, a hairline border, and the ring-free panel shadow. */
export function Card({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  const theme = useTheme();
  return (
    <View
      style={[
        {
          backgroundColor: theme.colors.bgPanel,
          borderColor: theme.colors.borderMuted,
          borderWidth: 1,
          borderRadius: theme.radii.md,
        },
        theme.shadow,
        style,
      ]}
    >
      {children}
    </View>
  );
}

/** A tappable list row: leading icon, label, optional trailing content. */
export function Row({
  icon: Icon, iconColor, title, subtitle, trailing, onPress, selected, tone,
}: {
  icon?: LucideIcon;
  iconColor?: string;
  title: string;
  subtitle?: string;
  trailing?: ReactNode;
  onPress?: () => void;
  selected?: boolean;
  tone?: Tone;
}) {
  const theme = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={!onPress}
      style={({ pressed }) => [
        styles.row,
        {
          borderRadius: theme.radii.md,
          paddingHorizontal: theme.space[2],
          paddingVertical: theme.space[2],
          gap: theme.space[2],
          backgroundColor: selected ? theme.colors.blueSubtle : pressed ? theme.colors.bgSubtle : "transparent",
        },
      ]}
    >
      {Icon ? <Icon size={16} color={iconColor ?? theme.colors.fgMuted} /> : null}
      <View style={styles.rowBody}>
        <Text variant="body" tone={tone ?? (selected ? "accent" : "default")} numberOfLines={1}>{title}</Text>
        {subtitle ? <Text variant="small" tone="subtle" numberOfLines={1}>{subtitle}</Text> : null}
      </View>
      {trailing}
    </Pressable>
  );
}

/** The 6px status dot the sidebar uses, shown only when there is something to say. */
export function StatusDot({ color }: { color: string }) {
  return <View style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: color }} />;
}

export function SectionLabel({ children }: { children: ReactNode }) {
  const theme = useTheme();
  return (
    <View style={{ paddingHorizontal: theme.space[1], paddingBottom: theme.space[1.5], paddingTop: theme.space[3] }}>
      <Text variant="sectionLabel" tone="muted">{children}</Text>
    </View>
  );
}

export function Divider() {
  const theme = useTheme();
  return <View style={{ height: 1, backgroundColor: theme.colors.borderMuted }} />;
}

/** Nothing here, and why. */
export function EmptyState({ icon: Icon, title, description }: {
  icon?: LucideIcon;
  title: string;
  description?: string;
}) {
  const theme = useTheme();
  return (
    <View style={[styles.empty, { gap: theme.space[2], padding: theme.space[6] }]}>
      {Icon ? <Icon size={22} color={theme.colors.fgSubtle} /> : null}
      <Text variant="title" tone="muted" align="center">{title}</Text>
      {description ? <Text variant="small" tone="subtle" align="center">{description}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  button: { flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6 },
  // Clipped to the icon's box so the indicator cannot change the row's height, and centred within it.
  busy: { alignItems: "center", justifyContent: "center", overflow: "hidden" },
  pill: { flexDirection: "row", alignItems: "center", paddingHorizontal: 6, paddingVertical: 2 },
  alert: { flexDirection: "row", alignItems: "flex-start", borderWidth: 1 },
  row: { flexDirection: "row", alignItems: "center" },
  rowBody: { flex: 1, gap: 1 },
  empty: { flex: 1, alignItems: "center", justifyContent: "center" },
});
