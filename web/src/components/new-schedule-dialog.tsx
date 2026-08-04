"use client";

// Creating a schedule without going through Settings.
//
// A schedule is a thing you *make*, like a workspace or a conversation, so it belongs beside
// them in the sidebar rather than only three screens into a settings dialog — where it was
// reachable but only by somebody who already knew it existed. The form itself is shared with
// the settings panel; this is the frame around it.

import { Dialog, Portal, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import type { AgentSummary } from "@/lib/api";
import { ScheduleForm } from "./schedule-form";

export function NewScheduleDialog({
  workspaceId,
  agents,
  open,
  onOpenChange,
}: {
  workspaceId: string;
  agents: AgentSummary[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const translation = useTranslations("SchedulesPanel");
  return (
    <Dialog.Root open={open} onOpenChange={(event) => onOpenChange(event.open)} placement="center">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content w="min(560px, calc(100vw - 16px))">
            <Dialog.Header display="flex" flexDirection="column" alignItems="flex-start" gap={1}>
              <Dialog.Title textStyle="panelTitle">{translation("add")}</Dialog.Title>
              <Text fontSize="xs" color="fg.muted">{translation("empty")}</Text>
            </Dialog.Header>
            <Dialog.Body>
              {/* Mounted only while open, so each visit starts from an empty draft rather than
                  from whatever was half-typed the last time the dialog was dismissed. */}
              {open && (
                <ScheduleForm
                  workspaceId={workspaceId}
                  agents={agents}
                  onCreated={() => onOpenChange(false)}
                  onCancel={() => onOpenChange(false)}
                />
              )}
            </Dialog.Body>
            <Dialog.CloseTrigger />
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
