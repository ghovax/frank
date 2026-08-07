"use client";

import {
  Alert,
  Box,
  Button,
  Flex,
  IconButton,
  Input,
  Skeleton,
  Spinner,
  Text,
} from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { LuFolder, LuFolderOpen, LuPlus, LuServer, LuTrash2 } from "react-icons/lu";
import { useTranslations } from "next-intl";
import {
  browseWorkingDirectory,
  fetchHomeDirectory,
  fetchHostHomeDirectory,
  type LocationInput,
  type PermissionMode,
  type SshHost,
} from "@/lib/api";
import { PermissionModeControl } from "./session-controls";
import { SimpleSelect, type SelectOption } from "./ui/simple-select";
import { swallowed } from "@/lib/swallowed";

// A fresh, empty location for the editor, whose name the server derives from the connection.
export function emptyLocation(): LocationInput {
  return {
    kind: "local",
    base_directory: "",
    host_alias: "",
    permission_mode: "ask",
  };
}

// The first overlap between two locations on one machine, which is redundant and ambiguous.
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
  for (let outerIndex = 0; outerIndex < normalized.length; outerIndex += 1) {
    for (let innerIndex = outerIndex + 1; innerIndex < normalized.length; innerIndex += 1) {
      const first = normalized[outerIndex];
      const second = normalized[innerIndex];
      if (first.machine !== second.machine || !first.path || !second.path) continue;
      if (first.path === second.path)
        return { key: "conflictSameDirectory", values: { directory: first.raw } };
      if (second.path.startsWith(`${first.path}/`))
        return { key: "conflictNested", values: { inner: second.raw, outer: first.raw } };
      if (first.path.startsWith(`${second.path}/`))
        return { key: "conflictNested", values: { inner: first.raw, outer: second.raw } };
    }
  }
  return null;
}

// The reusable location editor, shared by creation and by workspace settings.
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

  const hostItems = useMemo<SelectOption[]>(
    () =>
      hosts.map((host) => ({
        value: host.alias,
        label: `${host.alias} (${host.user ? `${host.user}@` : ""}${host.hostname}:${host.port})`,
      })),
    [hosts],
  );

  // Which machine this location points at, since a base directory is a path on that machine.
  const machine = value.kind === "remote" ? `remote:${(value.host_alias ?? "").trim()}` : "local";
  // Keep the latest value and handler in refs, so the effects below stay keyed only on what drives them.
  const valueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  // The machine whose path the field currently holds, seeded with the one the form opened on.
  const settledMachine = useRef(machine);
  const [resolvingHome, setResolvingHome] = useState(false);
  const [homeUnresolved, setHomeUnresolved] = useState(false);
  useEffect(() => {
    valueRef.current = value;
    onChangeRef.current = onChange;
  });

  // Fallback so a remote always has a host, for when hosts finish loading after the switch.
  useEffect(() => {
    const current = valueRef.current;
    if (current.kind === "remote" && !current.host_alias && hosts.length > 0) {
      onChangeRef.current({ ...current, host_alias: hosts[0].alias });
    }
  }, [value.kind, hosts]);

  // Prefill the base directory with that machine's home, as an editable starter.
  useEffect(() => {
    if (machine === settledMachine.current && valueRef.current.base_directory) return;
    let cancelled = false;
    const before = valueRef.current.base_directory;
    const alias = machine.startsWith("remote:") ? machine.slice("remote:".length) : "";
    setHomeUnresolved(false);
    setResolvingHome(true);
    (async () => {
      const home =
        machine === "local"
          ? (
              await fetchHomeDirectory().catch((caught) => {
                swallowed(
                  {
                    component: "environment-form",
                    operation: "read this machine's home directory",
                  },
                  caught,
                );
                return { path: "" };
              })
            ).path
          : alias
            ? await fetchHostHomeDirectory(alias)
            : "";
      if (cancelled) return;
      setResolvingHome(false);
      // A remote whose home could not be read says so rather than leaving an empty box.
      setHomeUnresolved(machine !== "local" && !!alias && !home);
      settledMachine.current = machine;
      const latest = valueRef.current;
      if (latest.base_directory !== before) return;
      if (home !== latest.base_directory) onChangeRef.current({ ...latest, base_directory: home });
    })();
    return () => {
      cancelled = true;
      setResolvingHome(false);
    };
  }, [machine]);

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
          // Switching to remote selects the first host at once, with the effect below only backfilling.
          onClick={() =>
            set({ kind: "remote", host_alias: value.host_alias || hosts[0]?.alias || "" })
          }
        >
          <LuServer size={13} /> {translation("remote")}
        </Button>
        {onRemove && (
          <IconButton
            aria-label={translation("removeLocation")}
            variant="ghost"
            colorPalette="red"
            flexShrink={0}
            onClick={onRemove}
          >
            <LuTrash2 size={13} />
          </IconButton>
        )}
      </Flex>

      {value.kind === "remote" && (
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">{translation("host")}</Text>
          {hosts.length === 0 ? (
            <Alert.Root status="warning" size="sm" borderRadius="md" alignItems="center">
              <Alert.Indicator />
              <Alert.Content flex={1} minW={0}>
                <Alert.Description fontSize="xs">{translation("noHosts")}</Alert.Description>
              </Alert.Content>
            </Alert.Root>
          ) : (
            <SimpleSelect
              items={hostItems}
              value={value.host_alias ?? ""}
              onValueChange={(next) => set({ host_alias: next })}
            />
          )}
        </Flex>
      )}

      <Flex direction="column" gap={1}>
        <Flex align="center" gap={2}>
          <Text textStyle="fieldLabel">{translation("baseDirectory")}</Text>
          {resolvingHome && <Spinner size="xs" color="fg.subtle" />}
        </Flex>
        <Flex gap={2}>
          <Input
            flex={1}
            value={value.base_directory}
            onChange={(event) => set({ base_directory: event.target.value })}
            placeholder={
              value.kind === "remote" ? "/srv/payments-service" : "/Users/you/code/payments-service"
            }
          />
          {value.kind === "local" && (
            <Button variant="outline" flexShrink={0} onClick={pickFolder}>
              <LuFolderOpen size={14} /> {translation("openFolder")}
            </Button>
          )}
        </Flex>
        {homeUnresolved && (
          <Alert.Root status="warning" size="sm" borderRadius="md" alignItems="center" mt={1}>
            <Alert.Indicator />
            <Alert.Content flex={1} minW={0}>
              <Alert.Description fontSize="xs">{translation("homeUnresolved")}</Alert.Description>
            </Alert.Content>
          </Alert.Root>
        )}
      </Flex>

      {showPermission && (
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">{translation("permissionMode")}</Text>
          {/* The same control the composer carries, so one picker serves wherever a permission mode is chosen. */}
          <PermissionModeControl
            layout="field"
            value={(value.permission_mode as PermissionMode) ?? "ask"}
            onChange={(next) => {
              if (next) set({ permission_mode: next });
            }}
          />
        </Flex>
      )}
    </Flex>
  );
}

// A stack of editable location forms with an inline overlap warning, shared by creation and settings.
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
      <Flex
        data-layout="location-editor-loading"
        direction="column"
        gap={3}
        aria-label={translation("loadingLocations")}
      >
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
