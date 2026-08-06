"use client";

// The form that describes one schedule, its own component because two places create one.

import { Alert, Button, Flex, Input, Text, Textarea } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";
import { createSchedule, type AgentSummary, type PermissionMode } from "@/lib/api";
import cronstrue from "cronstrue";
// Registers Japanese with the English core, rather than pulling in all thirty-odd locales for two.
import "cronstrue/locales/ja";
import { useLocale } from "@/lib/i18n/locale-provider";
import { AgentSelectControl, PermissionModeControl } from "./session-controls";
import { TimezoneSelect, currentZone } from "./ui/timezone-select";
import { toaster } from "./ui/toaster";
import { errorMessage } from "@/lib/errors";

type Draft = {
  name: string;
  cron: string;
  prompt: string;
  agent: string;
  // Starts at manual approvals, which matters most here because a schedule fires with nobody watching.
  permissionMode: PermissionMode;
  timezone: string;
};

function emptyDraft(agent: string): Draft {
  return { name: "", cron: "", prompt: "", agent, permissionMode: "ask", timezone: currentZone() };
}

// What a cron expression actually says, in the reader's own language.
type CronReading =
  | { kind: "described"; text: string }
  | { kind: "invalid"; reason: string };

function readCron(expression: string, locale: string): CronReading | null {
  if (expression.trim() === "") return null;
  try {
    return {
      kind: "described",
      // 24-hour, because the timezone field, the presets and the schedule list all speak that way.
      text: cronstrue.toString(expression, { locale, use24HourTimeFormat: true }),
    };
  } catch (caught) {
    // `String(caught)` rather than `.message`, since the library throws a string rather than an error.
    const reason = String(caught).replace(/^Error:\s*/, "");
    return { kind: "invalid", reason };
  }
}

// One field: its label above, its control below, and whatever it has to say underneath.
function ScheduleField({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <Flex direction="column" gap={1} minW={0}>
      <Text textStyle="fieldLabel">{label}</Text>
      {children}
      {hint}
    </Flex>
  );
}

export function ScheduleForm({
  workspaceId,
  agents,
  onCreated,
  onCancel,
}: {
  workspaceId: string;
  agents: AgentSummary[];
  onCreated: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const translation = useTranslations("SchedulesPanel");
  const { locale } = useLocale();
  const [draft, setDraft] = useState<Draft>(() => emptyDraft(agents[0]?.id ?? ""));
  const [saving, setSaving] = useState(false);

  const cronPresets: { cron: string; label: string }[] = [
    { cron: "0 * * * *", label: translation("presetHourly") },
    { cron: "0 9 * * *", label: translation("presetDaily") },
    { cron: "0 9 * * MON-FRI", label: translation("presetWeekdays") },
    { cron: "0 9 * * MON", label: translation("presetWeekly") },
  ];
  const reading = readCron(draft.cron, locale);
  // Required-ness is expressed by the button being unavailable rather than by a failure after the fact.
  const complete =
    draft.name.trim() !== "" && draft.cron.trim() !== "" && draft.prompt.trim() !== "" &&
    draft.agent !== "" && draft.timezone.trim() !== "" && reading?.kind === "described";

  async function handleCreate() {
    if (!complete) return;
    setSaving(true);
    try {
      await createSchedule({
        workspace_id: workspaceId,
        name: draft.name.trim(),
        cron: draft.cron.trim(),
        prompt: draft.prompt.trim(),
        agent: draft.agent,
        permission_mode: draft.permissionMode,
        timezone: draft.timezone.trim(),
        working_directory: "",
      });
      await onCreated();
    } catch (error) {
      // The daemon's own sentence, said while somebody is still here to read it.
      toaster.create({ type: "error", title: translation("createError"), description: errorMessage(error), closable: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Flex direction="column" gap={3}>
      <ScheduleField label={translation("labelName")}>
        <Input placeholder={translation("namePlaceholder")} value={draft.name}
               onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
      </ScheduleField>

      <ScheduleField
        label={translation("labelCron")}
        // Always an alert rather than a caption, since this line answers the only question that matters.
        hint={
          <Alert.Root
            status={reading?.kind === "invalid" ? "warning" : "info"}
            size="sm"
            borderRadius="md"
            alignItems="center"
            mt={1}
          >
            <Alert.Indicator />
            <Alert.Content flex={1} minW={0}>
              <Alert.Description fontSize="xs">
                {reading === null
                  ? translation("cronFields")
                  : reading.kind === "described"
                    ? reading.text
                    : reading.reason}
              </Alert.Description>
            </Alert.Content>
          </Alert.Root>
        }
      >
        <Input
          fontFamily="var(--app-font-mono)"
          placeholder="0 9 * * MON-FRI"
          value={draft.cron}
          onChange={(event) => setDraft({ ...draft, cron: event.target.value })}
        />
        <Flex gap={1.5} wrap="wrap" pt={1}>
          {cronPresets.map((preset) => (
            <Button
              key={preset.cron}
              variant={draft.cron.trim() === preset.cron ? "subtle" : "outline"}
              colorPalette={draft.cron.trim() === preset.cron ? "blue" : undefined}
              onClick={() => setDraft({ ...draft, cron: preset.cron })}
            >
              {preset.label}
            </Button>
          ))}
        </Flex>
      </ScheduleField>

      {/* Beside the expression, not below the prompt. `0 9 * * MON-FRI` is meaningless without
          the clock it is read against, so the two belong to one question. */}
      <ScheduleField label={translation("labelTimezone")}>
        <TimezoneSelect
          value={draft.timezone}
          onChange={(zone) => setDraft({ ...draft, timezone: zone })}
          placeholder={translation("timezonePlaceholder")}
          currentLabel={translation("timezoneCurrent")}
        />
      </ScheduleField>

      <ScheduleField label={translation("labelPrompt")}>
        <Textarea rows={3} placeholder={translation("promptPlaceholder")} value={draft.prompt}
                  onChange={(event) => setDraft({ ...draft, prompt: event.target.value })} />
      </ScheduleField>

      {/* One per row rather than side by side. Sharing the width gave each control half a
          dialog, which the controls themselves did not need — but the permission hint did: the
          sentence explaining that an unattended run under manual approvals parks on the first
          decision and waits forever was wrapping to four lines in a narrow column, which is how
          a warning turns into wallpaper. */}
      <ScheduleField label={translation("labelAgent")}>
        <AgentSelectControl
          layout="field"
          agents={agents}
          value={draft.agent}
          onChange={(next) => setDraft({ ...draft, agent: next })}
          placeholder={translation("labelAgent")}
        />
      </ScheduleField>

      <ScheduleField label={translation("labelPermission")} hint={
        <Text fontSize="xs" color="fg.muted">{translation("modeHint")}</Text>
      }>
        <PermissionModeControl
          layout="field"
          value={draft.permissionMode}
          onChange={(next) => { if (next) setDraft({ ...draft, permissionMode: next }); }}
        />
      </ScheduleField>

      <Flex justify="flex-end" gap={2} mt={1}>
        <Button variant="ghost" onClick={onCancel} disabled={saving}>{translation("cancel")}</Button>
        <Button colorPalette="blue" disabled={!complete || saving} loading={saving}
                onClick={() => void handleCreate()}>
          {translation("create")}
        </Button>
      </Flex>
    </Flex>
  );
}
