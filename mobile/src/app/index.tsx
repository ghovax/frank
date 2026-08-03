/**
 * The machines this phone knows, and the way into one of them.
 *
 * This is the app's home, and it is the answer to a question the interface cannot answer for
 * itself: *which* Frank. The interface belongs to one machine — it is served by that machine, it
 * talks to that machine, and it has no idea the others exist — so choosing between them is the
 * one navigation that has to live in the shell.
 *
 * Which is why it is the root of the stack rather than a panel over the top. Tapping a machine
 * pushes the interface, and the edge swipe every iOS app already has brings you back here. That
 * costs the page its own back gesture, which it can afford: it is a single-page application with
 * its own history controls, and it never needed a gesture to move within itself.
 *
 * A phone with one machine paired still sees this screen. The alternative was to skip it when
 * there is only one, and that trades a tap for a surprise — a screen that appears the day you
 * pair a second machine, in a place you have never seen it.
 */

import { router } from "expo-router";
import { Plus, Trash2 } from "lucide-react-native";
import { useState } from "react";
import { Alert as SystemAlert, ScrollView, StyleSheet, View } from "react-native";
import { useTranslations } from "use-intl";

import { FrankMark } from "../components/frank-mark";
import { Button, EmptyState, Row, StatusDot, Text } from "../components/ui";
import { useConnection, type Pairing } from "../lib/connection";
import { useTheme } from "../theme";
import { useEdgeInsets } from "../theme/insets";

export default function MachinesScreen() {
  const translation = useTranslations("MachinesScreen");
  const theme = useTheme();
  const insets = useEdgeInsets();
  const { machines, select, forget } = useConnection();
  const [busy, setBusy] = useState("");

  const open = (machine: Pairing) => {
    setBusy(machine.endpoint);
    select(machine.endpoint);
    router.push("/interface");
    // Cleared on the way out rather than on arrival: the push is synchronous and the probe is
    // not, so a spinner left running here would still be turning behind the screen on top of it.
    setTimeout(() => setBusy(""), 600);
  };

  /**
   * Confirmed, because forgetting a machine throws away its token.
   *
   * There is no undo — the token is the only copy this phone has, and getting another means
   * being at the machine to run `frank reach pair`. A swipe that quietly did this would be a
   * gesture whose cost is a walk to another room.
   */
  const confirmForget = (machine: Pairing) => {
    SystemAlert.alert(
      translation("forgetTitle", { machine: machine.name }),
      translation("forgetBody"),
      [
        { text: translation("cancel"), style: "cancel" },
        { text: translation("forget"), style: "destructive", onPress: () => void forget(machine.endpoint) },
      ],
    );
  };

  return (
    <View style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <View style={[styles.header, { paddingTop: insets.top + theme.space[4], paddingHorizontal: theme.space[4], paddingBottom: theme.space[3], gap: theme.space[3] }]}>
        <FrankMark size={26} color={theme.colors.fg} />
        <Text variant="heading" style={{ flex: 1 }}>{translation("title")}</Text>
      </View>

      <ScrollView
        contentContainerStyle={{
          paddingHorizontal: theme.space[3],
          paddingBottom: insets.bottom + theme.space[6],
          gap: theme.space[2],
          flexGrow: 1,
        }}
      >
        {machines.length === 0 ? (
          <EmptyState title={translation("noneTitle")} description={translation("noneBody")} />
        ) : (
          machines.map((machine) => (
            <Row
              key={machine.endpoint}
              title={machine.name}
              // The address, because it is what distinguishes two machines a person has given
              // similar names, and because seeing it is how you notice you paired the wrong one.
              subtitle={machine.endpoint.replace(/^https:\/\//, "")}
              onPress={() => open(machine)}
              trailing={
                <View style={{ flexDirection: "row", alignItems: "center", gap: theme.space[3] }}>
                  {busy === machine.endpoint ? <StatusDot color={theme.colors.blueSolid} /> : null}
                  <Button
                    icon={Trash2}
                    variant="ghost"
                    tone="danger"
                    onPress={() => confirmForget(machine)}
                  />
                </View>
              }
            />
          ))
        )}
      </ScrollView>

      <View style={{ paddingHorizontal: theme.space[4], paddingBottom: insets.bottom + theme.space[4] }}>
        <Button
          label={translation("pairAnother")}
          icon={Plus}
          full
          variant={machines.length === 0 ? "solid" : "outline"}
          tone={machines.length === 0 ? "accent" : "neutral"}
          onPress={() => router.push("/pair")}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  header: { flexDirection: "row", alignItems: "center" },
});
