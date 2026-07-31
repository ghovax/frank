"use client";

import { Button, Flex, IconButton, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import { LuPause, LuPlay, LuPlus, LuTrash2 } from "react-icons/lu";
import {
  deleteSchedule,
  listSchedules,
  runSchedule,
  setScheduleEnabled,
  subscribeEvents,
  type AgentSummary,
  type Schedule,
} from "@/lib/api";
import { swallowed } from "@/lib/swallowed";
import { ScheduleForm } from "./schedule-form";
import { Pill } from "./ui/pill";
import { toaster } from "./ui/toaster";
import { errorMessage } from "@/lib/errors";

export function SchedulesPanel({ workspaceId, agents }: { workspaceId: string; agents: AgentSummary[] }) {
  const translation = useTranslations("SchedulesPanel");
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState(false);

  const reload = useCallback(async () => {
    try {
      setSchedules(await listSchedules(workspaceId));
      setFailed(false);
    } catch (error) {
      setFailed(true);
      swallowed({ component: "schedules-panel", operation: "list the schedules" }, error);
    }
  }, [workspaceId]);

  useEffect(() => {
    void reload();
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "schedules_changed") void reload();
    });
    return () => {
      unsubscribe();
    };
  }, [reload]);

  async function handleToggle(schedule: Schedule) {
    setBusy(schedule.id + "toggle");
    try {
      await setScheduleEnabled(schedule.id, !schedule.enabled);
      await reload();
    } catch (error) {
      toaster.create({ type: "error", title: translation("updateError"), description: errorMessage(error), closable: true });
    } finally {
      setBusy("");
    }
  }

  async function handleDelete(schedule: Schedule) {
    setBusy(schedule.id + "delete");
    try {
      await deleteSchedule(schedule.id);
      await reload();
    } catch (error) {
      toaster.create({ type: "error", title: translation("deleteError"), description: errorMessage(error), closable: true });
    } finally {
      setBusy("");
    }
  }

  async function handleRun(schedule: Schedule) {
    setBusy(schedule.id + "run");
    try {
      const after = await runSchedule(schedule.id);
      toaster.create({
        type: after.last_error ? "error" : "success",
        title: after.last_error ? translation("runFailed") : translation("runStarted"),
        description: after.last_error || after.last_session_id,
        closable: true,
      });
      await reload();
    } catch (error) {
      toaster.create({ type: "error", title: translation("runFailed"), description: errorMessage(error), closable: true });
    } finally {
      setBusy("");
    }
  }

  function nextFiring(schedule: Schedule): string {
    if (!schedule.enabled) return translation("paused");
    if (!schedule.next_firing) return "—";
    const at = new Date(schedule.next_firing);
    return Number.isNaN(at.getTime()) ? "—" : at.toLocaleString();
  }

  if (failed) return <Text fontSize="sm" color="red.fg">{translation("loadError")}</Text>;

  return (
    <Flex direction="column" gap={3} w="100%">
      {schedules.length === 0 && !adding ? (
        <Text fontSize="sm" color="fg.muted">{translation("empty")}</Text>
      ) : null}

      {schedules.map((schedule) => (
        <Flex key={schedule.id} align="center" justify="space-between" gap={3}
              borderWidth="1px" borderColor="border" borderRadius="md" p={3}>
          <Flex direction="column" gap={1} minW={0}>
            <Flex align="center" gap={2}>
              <Pill colorPalette={schedule.enabled ? "teal" : "gray"}>{schedule.cron}</Pill>
              <Text fontWeight="medium">{schedule.name}</Text>
              <Pill colorPalette={schedule.permission_mode === "auto" ? "orange" : "gray"}>
                {schedule.permission_mode}
              </Pill>
            </Flex>
            <Text fontSize="xs" color="fg.muted" truncate>{schedule.prompt}</Text>
            <Text fontSize="xs" color="fg.muted" truncate>
              {translation("next")}: {nextFiring(schedule)} · {schedule.timezone} · {schedule.agent}
            </Text>
            {schedule.last_error ? (
              <Text fontSize="xs" color="red.fg" truncate>{schedule.last_error}</Text>
            ) : null}
          </Flex>
          <Flex align="center" gap={1}>
            <IconButton aria-label={translation("runNow")} variant="ghost"
                        loading={busy === schedule.id + "run"} onClick={() => void handleRun(schedule)}>
              <LuPlay size={13} />
            </IconButton>
            <IconButton aria-label={schedule.enabled ? translation("pause") : translation("resume")}
                        variant="ghost" loading={busy === schedule.id + "toggle"}
                        onClick={() => void handleToggle(schedule)}>
              {schedule.enabled ? <LuPause size={13} /> : <LuPlay size={13} />}
            </IconButton>
            <IconButton aria-label={translation("delete")} variant="ghost" colorPalette="red"
                        loading={busy === schedule.id + "delete"} onClick={() => void handleDelete(schedule)}>
              <LuTrash2 size={13} />
            </IconButton>
          </Flex>
        </Flex>
      ))}

      {adding ? (
        <Flex direction="column" borderWidth="1px" borderColor="border" borderRadius="md" p={3}>
          <ScheduleForm
            workspaceId={workspaceId}
            agents={agents}
            onCreated={async () => {
              setAdding(false);
              await reload();
            }}
            onCancel={() => setAdding(false)}
          />
        </Flex>
      ) : (
        <Flex justify="flex-end" mt={1}>
          <Button variant="subtle" colorPalette="blue" onClick={() => setAdding(true)}>
            <LuPlus size={13} /> {translation("add")}
          </Button>
        </Flex>
      )}
    </Flex>
  );
}
