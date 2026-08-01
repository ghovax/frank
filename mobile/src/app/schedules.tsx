/**
 * The machine's schedules: what it will do on its own, and when.
 *
 * Read, toggle and run — not create. Writing a cron expression on a phone keyboard is a thing
 * nobody wants to do, and the three verbs here are the ones you actually reach for away from the
 * desk: check that the nightly job is still on, turn one off because you are travelling, or make
 * one run now because you need its answer before it was due.
 */

import { Clock, Play, Trash2, X } from "lucide-react-native";
import { useCallback, useEffect, useState } from "react";
import { Pressable, RefreshControl, ScrollView, StyleSheet, Switch, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Card, EmptyState, Pill, Text } from "../components/ui";
import {
  deleteSchedule, listSchedules, runSchedule, setScheduleEnabled, subscribeEvents,
  type Schedule,
} from "../lib/api";
import { useConnection } from "../lib/connection";
import { goBack } from "../lib/navigation";
import { useTheme } from "../theme";

export default function SchedulesScreen() {
  const theme = useTheme();
  const insets = useSafeAreaInsets();
  const { status } = useConnection();
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const reload = useCallback(async () => {
    if (status !== "online") return;
    try {
      setSchedules(await listSchedules());
    } catch {
      // The banner on the session list owns saying the machine is unreachable.
    } finally {
      setLoaded(true);
    }
  }, [status]);

  useEffect(() => {
    // As on the session list: the state change is behind an await the analysis cannot follow.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
  }, [reload]);
  useEffect(() => {
    if (status !== "online") return;
    return subscribeEvents((event) => { if (event === "schedules_changed") void reload(); });
  }, [status, reload]);

  return (
    <View style={{ flex: 1, backgroundColor: theme.colors.bg, paddingTop: insets.top + theme.space[2] }}>
      <View style={[styles.bar, { paddingHorizontal: theme.space[4], paddingBottom: theme.space[2] }]}>
        <Text variant="heading" style={{ flex: 1 }}>Schedules</Text>
        <Pressable onPress={() => goBack()} hitSlop={12}>
          <X size={22} color={theme.colors.fgMuted} />
        </Pressable>
      </View>

      <ScrollView
        contentContainerStyle={{
          paddingHorizontal: theme.space[3],
          paddingBottom: insets.bottom + theme.space[8],
          gap: theme.space[2],
          flexGrow: 1,
        }}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            tintColor={theme.colors.fgMuted}
            onRefresh={() => { setRefreshing(true); void reload().finally(() => setRefreshing(false)); }}
          />
        }
      >
        {loaded && schedules.length === 0 ? (
          <EmptyState
            icon={Clock}
            title="Nothing is scheduled"
            description="Schedules are written at the machine. They will show up here once they exist."
          />
        ) : (
          schedules.map((schedule) => (
            <Card key={schedule.id} style={{ padding: theme.space[3], gap: theme.space[2] }}>
              <View style={[styles.line, { gap: theme.space[2] }]}>
                <Text variant="title" style={{ flex: 1 }} numberOfLines={1}>{schedule.name}</Text>
                <Switch
                  value={schedule.enabled}
                  onValueChange={(next) => {
                    setSchedules((previous) =>
                      previous.map((entry) => (entry.id === schedule.id ? { ...entry, enabled: next } : entry)));
                    void setScheduleEnabled(schedule.id, next).catch(() => void reload());
                  }}
                  trackColor={{ true: theme.colors.blueSolid, false: theme.colors.bgEmphasized }}
                />
              </View>

              <Text variant="small" tone="muted" numberOfLines={2}>{schedule.prompt}</Text>

              <View style={[styles.line, { gap: theme.space[1.5], flexWrap: "wrap" }]}>
                <Pill label={schedule.cron} icon={Clock} />
                <Pill label={schedule.agent} />
                {schedule.next_firing ? <Pill label={`next ${relative(schedule.next_firing)}`} tone="accent" /> : null}
                {schedule.last_error ? <Pill label="last run failed" tone="danger" /> : null}
              </View>

              <View style={{ flexDirection: "row", gap: theme.space[2] }}>
                <Pressable
                  onPress={() => void runSchedule(schedule.id)}
                  style={[styles.action, { gap: theme.space[1.5], paddingVertical: theme.space[1.5] }]}
                >
                  <Play size={14} color={theme.colors.fgMuted} />
                  <Text variant="label" tone="muted">Run now</Text>
                </Pressable>
                <Pressable
                  onPress={() => { void deleteSchedule(schedule.id).then(reload); }}
                  style={[styles.action, { gap: theme.space[1.5], paddingVertical: theme.space[1.5] }]}
                >
                  <Trash2 size={14} color={theme.colors.redFg} />
                  <Text variant="label" tone="danger">Delete</Text>
                </Pressable>
              </View>
            </Card>
          ))
        )}
      </ScrollView>
    </View>
  );
}

/** "in 4 hours", rather than a timestamp somebody has to subtract from now themselves. */
function relative(iso: string): string {
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return iso;
  const minutes = Math.round((target - Date.now()) / 60000);
  if (minutes < 1) return "any moment";
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `in ${hours}h`;
  return `in ${Math.round(hours / 24)}d`;
}

const styles = StyleSheet.create({
  bar: { flexDirection: "row", alignItems: "center" },
  line: { flexDirection: "row", alignItems: "center" },
  action: { flexDirection: "row", alignItems: "center" },
});
