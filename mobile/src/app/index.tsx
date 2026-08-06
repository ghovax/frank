/** The machines this phone knows, and the way into one of them. */

import { router } from "expo-router";
import { Plus, Trash2 } from "lucide-react-native";
import { useState } from "react";
import { Alert as SystemAlert, ScrollView, StyleSheet, View } from "react-native";
import { useTranslations } from "use-intl";

import { LangMeshMark } from "../components/langmesh-mark";
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
    // Cleared on the way out rather than on arrival, since the push is synchronous and the probe is not.
    setTimeout(() => setBusy(""), 600);
  };

  /** Confirmed, because forgetting a machine throws away the only copy of its token. */
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
        <LangMeshMark size={26} color={theme.colors.fg} />
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
              // The address, because it is what distinguishes two machines with similar names.
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
