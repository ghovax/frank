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
import { PERMISSION_MODES } from "@shared/controls";
import { ScheduleForm } from "./schedule-form";
import { Pill } from "./ui/pill";
import { useRelative } from "./ui/relative-time";
import { toaster } from "./ui/toaster";
import { errorMessage } from "@/lib/errors";

/** A schedule's approval mode, named and coloured as the picker names and colours it. */
function PermissionModePill({ mode }: { mode: string }) {
  const translation = useTranslations("SessionControls");
  const choice = PERMISSION_MODES.choices.find((entry) => entry.value === mode);
  if (choice === undefined) return <Pill colorPalette="gray">{mode}</Pill>;
  return (
    <Pill colorPalette={choice.palette ?? "gray"}>
      {translation(choice.labelKey as Parameters<typeof translation>[0])}
    </Pill>
  );
}

export function SchedulesPanel({
  workspaceId,
  agents,
}: {
  workspaceId: string;
  agents: AgentSummary[];
}) {
  const translation = useTranslations("SchedulesPanel");
  const relative = useRelative();
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState(false);

  // Reloading after a click or a daemon event, both of which happen once the panel is on screen.
  const reload = useCallback(async () => {
    try {
      setSchedules(await listSchedules(workspaceId));
      setFailed(false);
    } catch (error) {
      setFailed(true);
      swallowed({ component: "schedules-panel", operation: "list the schedules" }, error);
    }
  }, [workspaceId]);

  // Read once, then follow the daemon. Awaited here so a closed dialog can drop the answer.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const loaded = await listSchedules(workspaceId);
        if (cancelled) return;
        setSchedules(loaded);
        setFailed(false);
      } catch (error) {
        if (cancelled) return;
        setFailed(true);
        swallowed({ component: "schedules-panel", operation: "list the schedules" }, error);
      }
    })();
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "schedules_changed") void reload();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [reload, workspaceId]);

  async function handleToggle(schedule: Schedule) {
    setBusy(schedule.id + "toggle");
    try {
      await setScheduleEnabled(schedule.id, !schedule.enabled);
      await reload();
    } catch (error) {
      toaster.create({
        type: "error",
        title: translation("updateError"),
        description: errorMessage(error),
        closable: true,
      });
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
      toaster.create({
        type: "error",
        title: translation("deleteError"),
        description: errorMessage(error),
        closable: true,
      });
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
      toaster.create({
        type: "error",
        title: translation("runFailed"),
        description: errorMessage(error),
        closable: true,
      });
    } finally {
      setBusy("");
    }
  }

  function nextFiring(schedule: Schedule): string {
    if (!schedule.enabled) return translation("paused");
    if (!schedule.next_firing) return "—";
    return relative(schedule.next_firing) || "—";
  }

  if (failed)
    return (
      <Text fontSize="sm" color="red.fg">
        {translation("loadError")}
      </Text>
    );

  return (
    <Flex direction="column" gap={3} w="100%">
      {schedules.length === 0 && !adding ? (
        <Text fontSize="xs" color="fg.muted">
          {translation("empty")}
        </Text>
      ) : null}

      {schedules.map((schedule) => (
        <Flex
          key={schedule.id}
          align="center"
          justify="space-between"
          gap={3}
          borderWidth="1px"
          borderColor="border"
          borderRadius="md"
          p={3}
        >
          <Flex direction="column" gap={1} minW={0}>
            <Flex align="center" gap={2}>
              <Pill colorPalette={schedule.enabled ? "teal" : "gray"}>{schedule.cron}</Pill>
              <Text fontWeight="medium">{schedule.name}</Text>
              {/* Read off the same choice set the picker is built from, rather than colouring a raw string. */}
              <PermissionModePill mode={schedule.permission_mode} />
            </Flex>
            <Text fontSize="xs" color="fg.muted" truncate>
              {schedule.prompt}
            </Text>
            <Flex gap={3} fontSize="xs" color="fg.muted" minW={0} wrap="wrap">
              <Text truncate>
                {translation("next")} {nextFiring(schedule)}
              </Text>
              <Text truncate>{schedule.timezone}</Text>
              <Text truncate>{schedule.agent}</Text>
            </Flex>
            {schedule.last_error ? (
              <Text fontSize="xs" color="red.fg" truncate>
                {schedule.last_error}
              </Text>
            ) : null}
          </Flex>
          <Flex align="center" gap={1}>
            <IconButton
              aria-label={translation("runNow")}
              variant="ghost"
              loading={busy === schedule.id + "run"}
              onClick={() => void handleRun(schedule)}
            >
              <LuPlay size={13} />
            </IconButton>
            <IconButton
              aria-label={schedule.enabled ? translation("pause") : translation("resume")}
              variant="ghost"
              loading={busy === schedule.id + "toggle"}
              onClick={() => void handleToggle(schedule)}
            >
              {schedule.enabled ? <LuPause size={13} /> : <LuPlay size={13} />}
            </IconButton>
            <IconButton
              aria-label={translation("delete")}
              variant="ghost"
              colorPalette="red"
              loading={busy === schedule.id + "delete"}
              onClick={() => void handleDelete(schedule)}
            >
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
