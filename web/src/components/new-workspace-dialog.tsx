"use client";

import { Box, Button, Dialog, Portal, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { createWorkspace, type Workspace, type LocationInput, type SshHost } from "@/lib/api";
import { LocationEditorList, emptyLocation, locationConflict } from "./location-form";
import { toaster } from "./ui/toaster";
import { errorMessage } from "@/lib/errors";

// A workspace is an internal grouping of one or more environments — a folder on this machine or on an SSH host — so creation only asks where the agent will work.
export function NewWorkspaceDialog({
  hosts,
  hostsLoaded,
  open,
  onOpenChange,
  onCreated,
}: {
  hosts: SshHost[];
  hostsLoaded: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (workspace: Workspace) => void;
}) {
  const translation = useTranslations("NewWorkspaceDialog");
  const tc = useTranslations("Common");
  const [locations, setLocations] = useState<LocationInput[]>([emptyLocation()]);
  const [saving, setSaving] = useState(false);

  const locationValid = (location: LocationInput) =>
    location.base_directory.trim().length > 0 && (location.kind === "local" || (location.host_alias ?? "").length > 0);
  const conflict = locationConflict(locations);
  const canCreate = locations.length > 0 && locations.every(locationValid) && !conflict;

  const updateLocation = (index: number, next: LocationInput) =>
    setLocations((current) => current.map((location, position) => (position === index ? next : location)));
  const addLocation = () => setLocations((current) => [...current, emptyLocation()]);
  const removeLocation = (index: number) => setLocations((current) => current.filter((_, position) => position !== index));

  async function handleCreate() {
    setSaving(true);
    try {
      const workspace = await createWorkspace({ locations });
      onCreated(workspace);
      onOpenChange(false);
    } catch (error) {
      toaster.create({ type: "error", title: translation("createError"), description: errorMessage(error), closable: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(event) => onOpenChange(event.open)} placement="center">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content w="min(560px, calc(100vw - 16px))">
            <Dialog.Header display="flex" flexDirection="column" alignItems="flex-start" gap={1}>
              <Dialog.Title textStyle="panelTitle">{translation("title")}</Dialog.Title>
              <Text fontSize="sm" color="fg.muted">{translation("description")}</Text>
            </Dialog.Header>
            <Dialog.Body display="flex" flexDirection="column" gap={4}>
              <Box>
                <Text textStyle="panelTitle" mb={2}>{translation("environments")}</Text>
                <LocationEditorList hosts={hosts} locations={locations} onChange={updateLocation} onAdd={addLocation} onRemove={removeLocation} loading={!hostsLoaded} />
              </Box>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={() => onOpenChange(false)}>{tc("cancel")}</Button>
              <Button colorPalette="blue" disabled={!canCreate || saving} loading={saving} onClick={handleCreate}>
                {translation("create")}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
