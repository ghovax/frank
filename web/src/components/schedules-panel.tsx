"use client";

import { Box, Button, Flex, IconButton, Input, Text, Textarea } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState } from "react";
import { LuPause, LuPlay, LuPlus, LuTrash2 } from "react-icons/lu";
import {
  createSchedule,
  deleteSchedule,
  listSchedules,
  runSchedule,
  setScheduleEnabled,
  subscribeEvents,
  type AgentSummary,
  type PermissionMode,
  type Schedule,
} from "@/lib/api";
import { swallowed } from "@/lib/swallowed";
import { Pill } from "./ui/pill";
import { SimpleSelect } from "./ui/simple-select";
import { toaster } from "./ui/toaster";

type Draft = {
  name: string;
  cron: string;
  prompt: string;
  agent: string;
  // Empty until chosen, and typed to allow that. Every other field here can be sensibly
  // guessed; this one cannot, because the run happens with nobody present and a mode nobody
  // chose is the whole risk. The daemon refuses a schedule that omits it.
  permissionMode: PermissionMode | "";
  timezone: string;
};

/** The machine's own zone — what "nine every weekday" means to the person typing it. */
function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function emptyDraft(agent: string): Draft {
  return { name: "", cron: "", prompt: "", agent, permissionMode: "", timezone: localTimezone() };
}

export function SchedulesPanel({ workspaceId, agents }: { workspaceId: string; agents: AgentSummary[] }) {
  const translation = useTranslations("SchedulesPanel");
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [draft, setDraft] = useState<Draft>(() => emptyDraft(agents[0]?.id ?? ""));
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState(false);

  const reload = useCallback(async () => {
    try {
      setSchedules(await listSchedules(workspaceId));
      setFailed(false);
    } catch (error) {
      setFailed(true);
      swallowed("schedules panel: could not list schedules", error);
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

  const agentItems = agents.map((agent) => ({ value: agent.id, label: agent.name || agent.id }));
  const modeItems = [
    { value: "read_only", label: translation("modeReadOnly") },
    { value: "default", label: translation("modeDefault") },
    { value: "auto", label: translation("modeAuto") },
  ];
  // Required-ness is expressed by the button being unavailable rather than by a failure after
  // the fact — including the mode, which has no default to fall back on.
  const complete =
    draft.name.trim() !== "" && draft.cron.trim() !== "" && draft.prompt.trim() !== "" &&
    draft.agent !== "" && draft.permissionMode !== "" && draft.timezone.trim() !== "";

  function reset() {
    setDraft(emptyDraft(agents[0]?.id ?? ""));
    setAdding(false);
  }

  async function handleCreate() {
    if (!complete) return;
    setBusy("create");
    try {
      await createSchedule({
        workspace_id: workspaceId,
        name: draft.name.trim(),
        cron: draft.cron.trim(),
        prompt: draft.prompt.trim(),
        agent: draft.agent,
        permission_mode: draft.permissionMode as PermissionMode,
        timezone: draft.timezone.trim(),
        working_directory: "",
      });
      reset();
      await reload();
    } catch (error) {
      // The daemon's own sentence — which cron expression, which timezone. A schedule is written
      // once and fires unattended, so the moment to say what is wrong is while somebody is here.
      toaster.create({ type: "error", title: translation("createError"), description: error instanceof Error ? error.message : "", closable: true });
    } finally {
      setBusy("");
    }
  }

  async function handleToggle(schedule: Schedule) {
    setBusy(schedule.id + "toggle");
    try {
      await setScheduleEnabled(schedule.id, !schedule.enabled);
      await reload();
    } catch (error) {
      toaster.create({ type: "error", title: translation("updateError"), description: error instanceof Error ? error.message : "", closable: true });
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
      toaster.create({ type: "error", title: translation("deleteError"), description: error instanceof Error ? error.message : "", closable: true });
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
      toaster.create({ type: "error", title: translation("runFailed"), description: error instanceof Error ? error.message : "", closable: true });
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
            <IconButton aria-label={translation("runNow")} size="xs" variant="ghost"
                        loading={busy === schedule.id + "run"} onClick={() => void handleRun(schedule)}>
              <LuPlay size={13} />
            </IconButton>
            <IconButton aria-label={schedule.enabled ? translation("pause") : translation("resume")}
                        size="xs" variant="ghost" loading={busy === schedule.id + "toggle"}
                        onClick={() => void handleToggle(schedule)}>
              {schedule.enabled ? <LuPause size={13} /> : <LuPlay size={13} />}
            </IconButton>
            <IconButton aria-label={translation("delete")} size="xs" variant="ghost" colorPalette="red"
                        loading={busy === schedule.id + "delete"} onClick={() => void handleDelete(schedule)}>
              <LuTrash2 size={13} />
            </IconButton>
          </Flex>
        </Flex>
      ))}

      {adding ? (
        <Flex direction="column" gap={2} borderWidth="1px" borderColor="border" borderRadius="md" p={3}>
          <Input size="sm" placeholder={translation("namePlaceholder")} value={draft.name}
                 onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
          <Input size="sm" placeholder={translation("cronPlaceholder")} value={draft.cron}
                 onChange={(event) => setDraft({ ...draft, cron: event.target.value })} />
          <Textarea size="sm" rows={3} placeholder={translation("promptPlaceholder")} value={draft.prompt}
                    onChange={(event) => setDraft({ ...draft, prompt: event.target.value })} />
          <Flex gap={2}>
            <Box flex="1">
              <SimpleSelect items={agentItems} value={draft.agent} placeholder={translation("agentPlaceholder")}
                            onValueChange={(next) => setDraft({ ...draft, agent: next })} />
            </Box>
            <Box flex="1">
              <SimpleSelect items={modeItems} value={draft.permissionMode} placeholder={translation("modePlaceholder")}
                            onValueChange={(next) => setDraft({ ...draft, permissionMode: next as PermissionMode })} />
            </Box>
          </Flex>
          <Input size="sm" value={draft.timezone}
                 onChange={(event) => setDraft({ ...draft, timezone: event.target.value })} />
          <Text fontSize="xs" color="fg.muted">{translation("modeHint")}</Text>
          <Flex justify="flex-end" gap={2} mt={1}>
            <Button size="sm" variant="ghost" onClick={reset}>{translation("cancel")}</Button>
            <Button size="sm" colorPalette="blue" disabled={!complete || busy === "create"}
                    loading={busy === "create"} onClick={() => void handleCreate()}>
              {translation("create")}
            </Button>
          </Flex>
        </Flex>
      ) : (
        <Flex justify="flex-end" mt={1}>
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            <LuPlus size={13} /> {translation("add")}
          </Button>
        </Flex>
      )}
    </Flex>
  );
}
