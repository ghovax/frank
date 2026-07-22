"use client";

import { Button, Flex, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  createLocation, deleteLocation, getProject, listSshHosts, updateLocation,
  type Location, type LocationInput, type SshHost, subscribeEvents,
} from "@/lib/api";
import { LocationEditorList, emptyLocation, locationConflict } from "./location-form";
import { toaster } from "./ui/toaster";

function locationToInput(location: Location): LocationInput {
  return {
    kind: location.kind,
    base_directory: location.base_directory,
    host_alias: location.host_alias,
    permission_mode: location.permission_mode,
  };
}

type LocationDraft = { id: string | null; value: LocationInput };

function draftsFrom(locations: Location[]): LocationDraft[] {
  return locations.map((location) => ({ id: location.id, value: locationToInput(location) }));
}

// The project-folder manager inside Settings. Each folder is an inline editable form stacked
// above the next, with an "Add folder" button below — no list-then-edit view. Edits are
// batched and persisted on Save (create new, update changed, delete removed).
export function ProjectLocationsPanel({ projectId }: { projectId: string }) {
  const translation = useTranslations("ProjectLocationsPanel");
  const [hosts, setHosts] = useState<SshHost[]>([]);
  const [original, setOriginal] = useState<Location[]>([]);
  const [drafts, setDrafts] = useState<LocationDraft[]>([]);
  const [saving, setSaving] = useState(false);
  const [loadedProjectId, setLoadedProjectId] = useState("");
  const [failedProjectId, setFailedProjectId] = useState("");

  const loadProject = useCallback(async () => {
    const project = await getProject(projectId);
    const locations = project?.locations ?? [];
    setOriginal(locations);
    setDrafts(draftsFrom(locations));
  }, [projectId]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getProject(projectId), listSshHosts()])
      .then(([project, nextHosts]) => {
        if (cancelled) return;
        const locations = project?.locations ?? [];
        setOriginal(locations);
        setDrafts(draftsFrom(locations));
        setHosts(nextHosts);
        setFailedProjectId("");
        setLoadedProjectId(projectId);
      })
      .catch(() => {
        if (cancelled) return;
        setFailedProjectId(projectId);
        setLoadedProjectId(projectId);
      });
    // Only re-read hosts live — never clobber in-progress location edits from a
    // projects_changed event (a save reloads explicitly).
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "hosts_changed") {
        listSshHosts().then((nextHosts) => { if (!cancelled) setHosts(nextHosts); }).catch(() => {});
      }
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [projectId]);

  const updateDraft = (index: number, value: LocationInput) =>
    setDrafts((current) => current.map((draft, position) => (position === index ? { ...draft, value } : draft)));
  const addDraft = () => setDrafts((current) => [...current, { id: null, value: emptyLocation() }]);
  const removeDraft = (index: number) => setDrafts((current) => current.filter((_, position) => position !== index));

  const draftValid = (draft: LocationDraft) =>
    draft.value.base_directory.trim().length > 0 && (draft.value.kind === "local" || (draft.value.host_alias ?? "").length > 0);
  const conflict = locationConflict(drafts.map((draft) => draft.value));
  const dirty = JSON.stringify(drafts) !== JSON.stringify(draftsFrom(original));
  const canSave = drafts.length > 0 && drafts.every(draftValid) && !conflict && dirty;

  async function handleSave() {
    setSaving(true);
    try {
      const keptIds = new Set(drafts.filter((draft) => draft.id).map((draft) => draft.id));
      for (const location of original) {
        if (!keptIds.has(location.id)) await deleteLocation(location.id);
      }
      for (const draft of drafts) {
        if (draft.id === null) {
          await createLocation(projectId, draft.value);
        } else {
          const before = original.find((location) => location.id === draft.id);
          if (before && JSON.stringify(locationToInput(before)) !== JSON.stringify(draft.value)) {
            await updateLocation(draft.id, draft.value);
          }
        }
      }
      await loadProject();
    } catch (error) {
      toaster.create({ type: "error", title: translation("saveError"), description: error instanceof Error ? error.message : "", closable: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Flex direction="column" gap={3} maxW="560px">
      {failedProjectId === projectId ? (
        <Text fontSize="sm" color="red.fg">{translation("loadError")}</Text>
      ) : (
        <>
          <LocationEditorList
            hosts={hosts}
            locations={drafts.map((draft) => draft.value)}
            onChange={updateDraft}
            onAdd={addDraft}
            onRemove={removeDraft}
            showPermission
            loading={loadedProjectId !== projectId}
          />
          {loadedProjectId === projectId ? (
      <Flex justify="flex-end" mt={1}>
              <Button colorPalette="blue" disabled={!canSave || saving} loading={saving} onClick={handleSave}>
                {translation("saveChanges")}
              </Button>
            </Flex>
          ) : null}
        </>
      )}
    </Flex>
  );
}
