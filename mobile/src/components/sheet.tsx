/**
 * A sheet from the bottom, which is what a phone uses where the desktop opens a popover.
 *
 * A popover points at the control that opened it, and that only reads as a relationship when
 * there is room around the control for the sheet to sit beside. On a phone there never is, so a
 * popover becomes a dialog in the middle of the screen with an arrow pointing at nothing. A
 * sheet at the bottom is also where a thumb already is.
 */

import type { ReactNode } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useTheme } from "../theme";
import { Text } from "./ui";

export function Sheet({
  open, onClose, title, children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}) {
  const theme = useTheme();
  const insets = useSafeAreaInsets();

  return (
    <Modal visible={open} transparent animationType="slide" onRequestClose={onClose} statusBarTranslucent>
      <Pressable style={styles.scrim} onPress={onClose} />
      <View
        style={{
          backgroundColor: theme.colors.bgPanel,
          borderTopLeftRadius: theme.radii.xl,
          borderTopRightRadius: theme.radii.xl,
          borderTopWidth: 1,
          borderColor: theme.colors.border,
          paddingBottom: insets.bottom + theme.space[3],
          maxHeight: "78%",
        }}
      >
        <View style={[styles.grabber, { backgroundColor: theme.colors.borderEmphasized, marginTop: theme.space[2] }]} />
        <View style={{ paddingHorizontal: theme.space[4], paddingVertical: theme.space[3] }}>
          <Text variant="title">{title}</Text>
        </View>
        <ScrollView contentContainerStyle={{ paddingHorizontal: theme.space[3], paddingBottom: theme.space[2] }}>
          {children}
        </ScrollView>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: { flex: 1, backgroundColor: "rgba(0,0,0,0.45)" },
  grabber: { alignSelf: "center", width: 36, height: 4, borderRadius: 2 },
});
