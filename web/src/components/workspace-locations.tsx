"use client";

import { Button, Flex, Text } from "@chakra-ui/react";
import { swallowed } from "@/lib/swallowed";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import {
  createLocation, deleteLocation, getWorkspace, listSshHosts, updateLocation,
  type Location, type LocationInput, type SshHost, subscribeEvents,
} from "@/lib/api";
import { LocationEditorList, emptyLocation, locationConflict } from "./location-form";
import { toaster } from "./ui/toaster";
import { errorMessage } from "@/lib/errors";

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

// The workspace-environment manager inside Settings. Each environment is an inline editable form
// stacked above the next, with an "Add environment" button below — no list-then-edit view. Edits are
// batched and persisted on Save (create new, update changed, delete removed).
export function WorkspaceLocationsPanel({ workspaceId }: { workspaceId: string }) {
  const translation = useTranslations("WorkspaceLocationsPanel");
  const [hosts, setHosts] = useState<SshHost[]>([]);
  const [original, setOriginal] = useState<Location[]>([]);
  const [drafts, setDrafts] = useState<LocationDraft[]>([]);
  const [saving, setSaving] = useState(false);
  const [loadedWorkspaceId, setLoadedWorkspaceId] = useState("");
  const [failedWorkspaceId, setFailedWorkspaceId] = useState("");

  const loadWorkspace = useCallback(async () => {
    const workspace = await getWorkspace(workspaceId);
    const locations = workspace?.locations ?? [];
    setOriginal(locations);
    setDrafts(draftsFrom(locations));
  }, [workspaceId]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getWorkspace(workspaceId), listSshHosts()])
      .then(([workspace, nextHosts]) => {
        if (cancelled) return;
        const locations = workspace?.locations ?? [];
        setOriginal(locations);
        setDrafts(draftsFrom(locations));
        setHosts(nextHosts);
        setFailedWorkspaceId("");
        setLoadedWorkspaceId(workspaceId);
      })
      .catch(() => {
        if (cancelled) return;
        setFailedWorkspaceId(workspaceId);
        setLoadedWorkspaceId(workspaceId);
      });
    // Only re-read hosts live — never clobber in-progress location edits from a
    // workspaces_changed event (a save reloads explicitly).
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "hosts_changed") {
        listSshHosts().then((nextHosts) => { if (!cancelled) setHosts(nextHosts); }).catch((caught) => swallowed({ component: "workspace-locations", operation: "list the SSH hosts" }, caught));
      }
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [workspaceId]);

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
          await createLocation(workspaceId, draft.value);
        } else {
          const before = original.find((location) => location.id === draft.id);
          if (before && JSON.stringify(locationToInput(before)) !== JSON.stringify(draft.value)) {
            await updateLocation(draft.id, draft.value);
          }
        }
      }
      await loadWorkspace();
    } catch (error) {
      toaster.create({ type: "error", title: translation("saveError"), description: errorMessage(error), closable: true });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Flex direction="column" gap={3} w="100%">
      {failedWorkspaceId === workspaceId ? (
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
            loading={loadedWorkspaceId !== workspaceId}
          />
          {loadedWorkspaceId === workspaceId ? (
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
