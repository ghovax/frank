"use client";

import { Box, Button, Dialog, Flex, Input, Portal, Text, Textarea } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { createProject, type Project, type LocationInput, type SshHost } from "@/lib/api";
import { LocationEditorList, emptyLocation, locationConflict } from "./location-form";
import { toaster } from "./ui/toaster";

// The New Project wizard: name + description + at least one location (required before the
// project exists). Extracted so both the sidebar project switcher and the manage-projects
// dialog can open it. Mount it only while open so its initializers give fresh state each time.
export function NewProjectDialog({
  hosts,
  open,
  onOpenChange,
  onCreated,
}: {
  hosts: SshHost[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (project: Project) => void;
}) {
  const t = useTranslations("ProjectsHome");
  const tc = useTranslations("Common");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [locations, setLocations] = useState<LocationInput[]>([emptyLocation()]);
  const [saving, setSaving] = useState(false);

  const locationValid = (location: LocationInput) =>
    location.base_directory.trim().length > 0 && (location.kind === "local" || (location.host_alias ?? "").length > 0);
  const conflict = locationConflict(locations);
  const canCreate = name.trim().length > 0 && locations.length > 0 && locations.every(locationValid) && !conflict;

  const updateLocation = (index: number, next: LocationInput) =>
    setLocations((current) => current.map((location, position) => (position === index ? next : location)));
  const addLocation = () => setLocations((current) => [...current, emptyLocation()]);
  const removeLocation = (index: number) => setLocations((current) => current.filter((_, position) => position !== index));

  async function handleCreate() {
    setSaving(true);
    try {
      const project = await createProject({ name: name.trim(), description: description.trim(), locations });
      onCreated(project);
      onOpenChange(false);
    } catch (error) {
      toaster.create({ type: "error", title: t("createError"), description: error instanceof Error ? error.message : "", closable: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(event) => onOpenChange(event.open)} placement="center">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content maxW="min(560px, 94vw)">
            <Dialog.Header>
              <Dialog.Title textStyle="panelTitle">{t("newProject")}</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body display="flex" flexDirection="column" gap={4}>
              <Flex direction="column" gap={1.5}>
                <Text textStyle="fieldLabel">{t("nameLabel")}</Text>
                <Input value={name} onChange={(event) => setName(event.target.value)} placeholder={t("namePlaceholder")} autoFocus />
              </Flex>
              <Flex direction="column" gap={1.5}>
                <Text textStyle="fieldLabel">{t("descriptionLabel")}</Text>
                <Textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} placeholder={t("descriptionPlaceholder")} />
              </Flex>
              <Box borderTop="1px solid" borderColor="border" pt={3}>
                <Text textStyle="panelTitle" mb={2}>{t("locations")}</Text>
                <LocationEditorList hosts={hosts} locations={locations} onChange={updateLocation} onAdd={addLocation} onRemove={removeLocation} />
              </Box>
            </Dialog.Body>
            <Dialog.Footer>
              <Button variant="outline" onClick={() => onOpenChange(false)}>{tc("cancel")}</Button>
              <Button colorPalette="blue" disabled={!canCreate || saving} loading={saving} onClick={handleCreate}>
                {t("createProject")}
              </Button>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
