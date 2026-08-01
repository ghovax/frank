"use client";

// The form that describes one schedule, and nothing else.
//
// Its own component because a schedule can be created from two places now — the Settings panel
// that lists them, and a dialog reached from the sidebar without opening Settings at all — and
// a form duplicated across two callers is a form that drifts: the cron reading gets a fix in
// one, the permission hint gets reworded in the other, and the two stop being the same thing
// people learned. Everything about *what a schedule is* lives here; both callers supply only
// the workspace it belongs to and what to do once one exists.

import { Alert, Button, Flex, Input, Text, Textarea } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";
import { createSchedule, type AgentSummary, type PermissionMode } from "@/lib/api";
import cronstrue from "cronstrue";
// Registers Japanese with the English core, rather than `cronstrue/i18n`, which carries all
// thirty-odd locales for the two this app has.
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
  // Starts at manual approvals — the same mode a new session starts under, and the most
  // restrictive of the three that can still do work. The daemon refuses a schedule with no
  // mode at all, so this field is never empty; the hint beside the control is what says why
  // the choice matters for a run nobody is watching.
  permissionMode: PermissionMode;
  timezone: string;
};

function emptyDraft(agent: string): Draft {
  return { name: "", cron: "", prompt: "", agent, permissionMode: "default", timezone: currentZone() };
}

// What a cron expression actually says, in the reader's own language.
//
// `cronstrue` rather than a reader of our own, and the reason is the shape of the problem
// rather than the size of the library. Cron is lists, ranges, steps and names across five
// fields that interact, so anything short of a parser recognises the handful of shapes its
// author thought of and answers everything else with a shrug — which is precisely what the
// version this replaced did: `0 9 * * MON-FRI` read back as "Every weekday at 09:00", while
// `0 9 * * MON-THU`, `30 6 * * SAT,SUN` and `0 */4 * * *` all fell through to "five fields:
// minute, hour, …", a sentence that restates the syntax to somebody who has just written it.
//
// A schedule fires unattended for months, so the one moment to discover that an expression
// does not say what its author meant is while they are still looking at it.
type CronReading =
  | { kind: "described"; text: string }
  | { kind: "invalid"; reason: string };

function readCron(expression: string, locale: string): CronReading | null {
  if (expression.trim() === "") return null;
  try {
    return {
      kind: "described",
      // 24-hour because the rest of this form is: the timezone field, the presets, and the
      // schedule list all speak in `09:00`.
      text: cronstrue.toString(expression, { locale, use24HourTimeFormat: true }),
    };
  } catch (caught) {
    // Deliberately `String(caught)` and not `caught.message`. cronstrue throws a *string*, so
    // `instanceof Error` is false and `.message` is `undefined` — which is what the field
    // would have displayed to somebody who mistyped a weekday. The prefix is stripped because
    // this is already rendered as a warning; the word "Error" twice is not more informative.
    const reason = String(caught).replace(/^Error:\s*/, "");
    return { kind: "invalid", reason };
  }
}

// One field: its label above, its control below, and whatever it has to say about itself
// underneath. The same shape the environment editor uses, so the two forms read alike.
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
  // Required-ness is expressed by the button being unavailable rather than by a failure after
  // the fact — including an expression that is not five fields, which the daemon would reject
  // anyway and which the reading below already names.
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
      // The daemon's own sentence — which cron expression, which timezone. A schedule is written
      // once and fires unattended, so the moment to say what is wrong is while somebody is here.
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
        // Always an alert, never a caption. This line is the answer to the only question that
        // matters about a cron expression — *when does this actually run* — and it was set in
        // muted 12px under the field, which is where a form puts things nobody needs to read.
        // A person checks this before trusting a job to fire unattended for months.
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
          placeholder={translation("agentPlaceholder")}
        />
      </ScheduleField>

      <ScheduleField label={translation("labelPermission")} hint={
        <Text fontSize="xs" color="fg.muted">{translation("modeHint")}</Text>
      }>
        <PermissionModeControl
          layout="field"
          value={draft.permissionMode}
          onChange={(next) => setDraft({ ...draft, permissionMode: next })}
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
