"use client";

import { Alert, Box, Button, Flex, IconButton, Input, Skeleton, Text } from "@chakra-ui/react";
import { useEffect, useMemo, useRef } from "react";
import { LuFolder, LuFolderOpen, LuPlus, LuServer, LuTrash2 } from "react-icons/lu";
import { useTranslations } from "next-intl";
import { browseWorkingDirectory, fetchHomeDirectory, fetchHostHomeDirectory, type LocationInput, type SshHost } from "@/lib/api";
import { SimpleSelect, type SelectOption } from "./ui/simple-select";

// A fresh, empty location for the wizard/editor. The name is derived server-side from the
// connection, and permission_mode defaults to manual approvals.
export function emptyLocation(): LocationInput {
  return {
    kind: "local",
    base_directory: "",
    host_alias: "",
    permission_mode: "default",
  };
}

// A structured description of the first overlap between two locations on the same machine
// — identical base directories, or one nested inside another — which is redundant and
// ambiguous. Locations on different machines (different host, or local vs remote) never
// conflict. Mirrors the server's authoritative check so the UI can warn before submitting.
// The conflict is returned as a translation key + values so the caller localizes the copy.
export type LocationConflict =
  | { key: "conflictSameDirectory"; values: { directory: string } }
  | { key: "conflictNested"; values: { inner: string; outer: string } };

export function locationConflict(
  locations: Array<{ kind: string; host_alias?: string; base_directory: string }>,
): LocationConflict | null {
  const normalized = locations.map((location) => ({
    machine: location.kind === "remote" ? `remote:${(location.host_alias ?? "").trim()}` : "local",
    path: location.base_directory.trim().replace(/\/+$/, ""),
    raw: location.base_directory.trim(),
  }));
  for (let i = 0; i < normalized.length; i += 1) {
    for (let j = i + 1; j < normalized.length; j += 1) {
      const a = normalized[i];
      const b = normalized[j];
      if (a.machine !== b.machine || !a.path || !b.path) continue;
      if (a.path === b.path) return { key: "conflictSameDirectory", values: { directory: a.raw } };
      if (b.path.startsWith(`${a.path}/`)) return { key: "conflictNested", values: { inner: b.raw, outer: a.raw } };
      if (a.path.startsWith(`${b.path}/`)) return { key: "conflictNested", values: { inner: a.raw, outer: b.raw } };
    }
  }
  return null;
}

// The reusable folder editor — used during creation and in project-folder settings.
// `hosts` come from ~/.ssh/config (remote only). `showPermission` reveals the permission
// mode control (shown when editing an existing location, hidden in the create wizard so
// creation stays about *where*, not policy). `onRemove` (optional) puts a remove control in
// the local/remote row.
export function LocationForm({
  hosts,
  value,
  onChange,
  showPermission = false,
  onRemove,
}: {
  hosts: SshHost[];
  value: LocationInput;
  onChange: (next: LocationInput) => void;
  showPermission?: boolean;
  onRemove?: () => void;
}) {
  const translation = useTranslations("LocationForm");
  const set = (patch: Partial<LocationInput>) => onChange({ ...value, ...patch });

  const permissionModeItems = useMemo<SelectOption[]>(
    () => [
      { value: "default", label: translation("permissionManual") },
      { value: "auto", label: translation("permissionAuto") },
      { value: "read_only", label: translation("permissionReadOnly") },
      { value: "bypass", label: translation("permissionBypass") },
    ],
    [translation],
  );

  const hostItems = useMemo<SelectOption[]>(
    () => hosts.map((host) => ({
      value: host.alias,
      label: `${host.alias} (${host.user ? `${host.user}@` : ""}${host.hostname}:${host.port})`,
    })),
    [hosts],
  );

  // Keep the latest value/onChange in refs (updated after each render, before the effects
  // below run) so those effects can stay keyed only on what actually drives them.
  const valueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  const lastAutoBaseDirectory = useRef("");
  useEffect(() => {
    valueRef.current = value;
    onChangeRef.current = onChange;
  });

  // Fallback so a remote location always has a host selected: if hosts finish loading
  // *after* the user already switched to remote (so the click handler couldn't pick one
  // yet), select the first host as soon as they arrive.
  useEffect(() => {
    const current = valueRef.current;
    if (current.kind === "remote" && !current.host_alias && hosts.length > 0) {
      onChangeRef.current({ ...current, host_alias: hosts[0].alias });
    }
  }, [value.kind, hosts]);

  // Prefill the base directory with the selected location's home directory as an editable
  // starter: the local machine's home for a local location, the host's home for a remote.
  // Only fills when the field is empty or still holds a previous auto value, so a path the
  // user typed is never clobbered.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const current = valueRef.current;
      const home = current.kind === "local"
        ? (await fetchHomeDirectory()).path
        : current.host_alias ? await fetchHostHomeDirectory(current.host_alias) : "";
      if (cancelled) return;
      const latest = valueRef.current;
      if (latest.base_directory === "" || latest.base_directory === lastAutoBaseDirectory.current) {
        lastAutoBaseDirectory.current = home;
        if (home !== latest.base_directory) onChangeRef.current({ ...latest, base_directory: home });
      }
    })();
    return () => { cancelled = true; };
  }, [value.kind, value.host_alias]);

  async function pickFolder() {
    try {
      const result = await browseWorkingDirectory();
      if (!result.cancelled && result.path) set({ base_directory: result.path });
    } catch {
      // picker unavailable or cancelled
    }
  }

  return (
    <Flex direction="column" gap={3}>
      <Flex align="center" gap={2}>
        <Button
          flex={1}
          variant={value.kind === "local" ? "subtle" : "outline"}
          colorPalette={value.kind === "local" ? "green" : undefined}
          onClick={() => set({ kind: "local" })}
        >
          <LuFolder size={13} /> {translation("local")}
        </Button>
        <Button
          flex={1}
          variant={value.kind === "remote" ? "subtle" : "outline"}
          colorPalette={value.kind === "remote" ? "blue" : undefined}
          // Switching to remote immediately selects the first host (if any are loaded)
          // rather than leaving the host empty; the effect below only backfills for the
          // case where hosts finish loading after the switch.
          onClick={() => set({ kind: "remote", host_alias: value.host_alias || hosts[0]?.alias || "" })}
        >
          <LuServer size={13} /> {translation("remote")}
        </Button>
        {onRemove && (
          <IconButton aria-label={translation("removeLocation")} variant="ghost" colorPalette="red" flexShrink={0} onClick={onRemove}>
            <LuTrash2 size={13} />
          </IconButton>
        )}
      </Flex>

      {value.kind === "remote" && (
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">{translation("host")}</Text>
          {hosts.length === 0 ? (
            <Text fontSize="2xs" color="orange.fg">{translation("noHosts")}</Text>
          ) : (
            <SimpleSelect items={hostItems} value={value.host_alias ?? ""} onValueChange={(next) => set({ host_alias: next })} />
          )}
        </Flex>
      )}

      <Flex direction="column" gap={1}>
        <Text textStyle="fieldLabel">{translation("baseDirectory")}</Text>
        <Flex gap={2}>
          <Input
            flex={1}
            value={value.base_directory}
            onChange={(event) => set({ base_directory: event.target.value })}
            placeholder={value.kind === "remote" ? "/srv/payments-service" : "/Users/you/code/payments-service"}
          />
          {value.kind === "local" && (
            <Button variant="outline" flexShrink={0} onClick={pickFolder}>
              <LuFolderOpen size={14} /> {translation("openFolder")}
            </Button>
          )}
        </Flex>
      </Flex>

      {showPermission && (
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">{translation("permissionMode")}</Text>
          <SimpleSelect items={permissionModeItems} value={value.permission_mode ?? "default"} onValueChange={(next) => set({ permission_mode: next })} />
        </Flex>
      )}
    </Flex>
  );
}

// A stack of editable folder forms with an "Add folder" button and an inline overlap
// warning. Creation and Settings share the same editing surface; only persistence differs.
export function LocationEditorList({
  hosts,
  locations,
  onChange,
  onAdd,
  onRemove,
  showPermission = false,
  loading = false,
}: {
  hosts: SshHost[];
  locations: LocationInput[];
  onChange: (index: number, value: LocationInput) => void;
  onAdd: () => void;
  onRemove: (index: number) => void;
  showPermission?: boolean;
  loading?: boolean;
}) {
  const translation = useTranslations("LocationForm");
  if (loading) {
    return (
      <Flex data-layout="location-editor-loading" direction="column" gap={3} aria-label={translation("loadingLocations")}>
        <Box borderWidth="1px" borderColor="border" borderRadius="md" p={3}>
          <Flex direction="column" gap={3}>
            <Flex gap={2}>
              <Skeleton h={8} flex={1} borderRadius="md" />
              <Skeleton h={8} flex={1} borderRadius="md" />
            </Flex>
            <Flex direction="column" gap={1}>
              <Skeleton h={5} w={24} borderRadius="sm" />
              <Skeleton h={8} w="full" borderRadius="md" />
            </Flex>
            {showPermission ? (
              <Flex direction="column" gap={1}>
                <Skeleton h={5} w={24} borderRadius="sm" />
                <Skeleton h={8} w="full" borderRadius="md" />
              </Flex>
            ) : null}
          </Flex>
        </Box>
        <Skeleton h={8} w="full" borderRadius="md" />
      </Flex>
    );
  }
  const conflict = locationConflict(locations);
  return (
    <Flex direction="column" gap={3}>
      {locations.map((location, index) => (
        <Box key={index} borderWidth="1px" borderColor="border" borderRadius="md" p={3}>
          <LocationForm
            hosts={hosts}
            value={location}
            onChange={(value) => onChange(index, value)}
            showPermission={showPermission}
            onRemove={locations.length > 1 ? () => onRemove(index) : undefined}
          />
        </Box>
      ))}
      <Button variant="subtle" colorPalette="blue" w="100%" onClick={onAdd}>
        <LuPlus size={14} /> {translation("addLocation")}
      </Button>
      {conflict && (
        <Alert.Root status="warning" size="sm" borderRadius="md" alignItems="center">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs">
              {conflict.key === "conflictSameDirectory"
                ? translation("conflictSameDirectory", conflict.values)
                : translation("conflictNested", conflict.values)}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      )}
    </Flex>
  );
}
