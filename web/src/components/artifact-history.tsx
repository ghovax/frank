"use client";

import { Box, Flex, Text, VStack } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { LuClock, LuHistory } from "react-icons/lu";
import { useFormatter, useTranslations } from "next-intl";
import { iconForFilePath } from "@/lib/file-icons";
import {
  fetchArtifactVersions,
  type ArtifactIndexEntry,
  type ArtifactScope,
  type ArtifactVersion,
} from "@/lib/api";
import { VersionBadge } from "./attachment-chips";
import { PanelBody, PanelEmptyState } from "./ui/panel";
import { Pill } from "./ui/pill";
import { SegmentedToggle } from "./ui/segmented-toggle";
import { Tooltip } from "./ui/tooltip";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";

type ChangeLabelKey = "changeAdded" | "changeDeleted" | "changeModified";

function changeTypeAppearance(changeType: "A" | "M" | "D"): { labelKey: ChangeLabelKey; palette: string } {
  if (changeType === "A") return { labelKey: "changeAdded", palette: "green" };
  if (changeType === "D") return { labelKey: "changeDeleted", palette: "red" };
  return { labelKey: "changeModified", palette: "blue" };
}

function ArtifactHistoryRow({
  entry,
  sessionId,
  scope,
  onOpen,
}: {
  entry: ArtifactIndexEntry;
  sessionId: string;
  scope: ArtifactScope;
  onOpen: (entry: ArtifactIndexEntry, versionId: string) => void;
}) {
  const translations = useTranslations("ChatPanel");
  const formatter = useFormatter();
  const [open, setOpen] = useState(false);
  const [versionState, setVersionState] = useState<{
    requestKey: string;
    versions: ArtifactVersion[];
  }>({ requestKey: "", versions: [] });
  const requestGenerationRef = useRef(0);
  const inFlightRequestKeyRef = useRef("");
  useEffect(() => () => {
    requestGenerationRef.current += 1;
  }, []);

  const requestKey = `${sessionId}|${scope}|${entry.gitDirectory}|${entry.relativePath}|${entry.latestCommit}|${entry.versionCount}`;
  const versions = versionState.requestKey === requestKey ? versionState.versions : [];
  const loading = open && versionState.requestKey !== requestKey;
  const latestAppearance = changeTypeAppearance(entry.latestChange);
  const FileIcon = iconForFilePath(entry.relativePath).icon;

  useEffect(() => {
    if (!open || versionState.requestKey === requestKey || inFlightRequestKeyRef.current === requestKey) return;
    inFlightRequestKeyRef.current = requestKey;
    const requestGeneration = ++requestGenerationRef.current;
    void fetchArtifactVersions(sessionId, entry.gitDirectory, entry.relativePath, scope)
      .then((loadedVersions) => {
        if (requestGenerationRef.current !== requestGeneration) return;
        setVersionState({
          requestKey,
          versions: [...loadedVersions].sort((left, right) => left.sequence - right.sequence),
        });
      })
      .catch(() => {
        if (requestGenerationRef.current !== requestGeneration) return;
        setVersionState({ requestKey, versions: [] });
      })
      .finally(() => {
        if (inFlightRequestKeyRef.current === requestKey) inFlightRequestKeyRef.current = "";
      });
  }, [open, requestKey, sessionId, entry.gitDirectory, entry.relativePath, scope, versionState.requestKey]);

  return (
    <DisclosureRow
      fill
      open={open}
      onOpenChange={setOpen}
      icon={<Box color="fg.muted"><FileIcon /></Box>}
      title={
        <Tooltip content={entry.relativePath} openDelay={350} positioning={{ placement: "left" }}>
          <Box minW={0}><DisclosureLabel>{entry.relativePath}</DisclosureLabel></Box>
        </Tooltip>
      }
      badges={
        <>
          <Pill colorPalette={latestAppearance.palette}>{translations(latestAppearance.labelKey)}</Pill>
          <VersionBadge number={entry.versionCount} active size="sm" />
        </>
      }
    >
      <Flex direction="column" gap={1}>
        {loading ? (
          <Text textStyle="fieldLabel" color="fg.subtle">{translations("loadingVersions")}</Text>
        ) : versions.length === 0 ? (
          <Text textStyle="fieldLabel" color="fg.subtle">{translations("noVersions")}</Text>
        ) : versions.map((version, versionIndex) => {
          const versionAppearance = changeTypeAppearance(version.changeType);
          const createdAt = version.createdAt && !Number.isNaN(new Date(version.createdAt).getTime())
            ? formatter.dateTime(new Date(version.createdAt), { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
            : translations("unknown");
          return (
            <DisclosureRow
              key={version.versionId}
              fill
              onActivate={() => onOpen(entry, version.versionId)}
              icon={<Box color="fg.muted"><LuClock /></Box>}
              title={<DisclosureLabel>{translations("versionNumber", { number: versionIndex + 1 })}</DisclosureLabel>}
              badges={
                <>
                  <Text textStyle="fieldLabel" color="fg.subtle">{createdAt}</Text>
                  <Pill colorPalette={versionAppearance.palette}>{translations(versionAppearance.labelKey)}</Pill>
                </>
              }
            />
          );
        })}
      </Flex>
    </DisclosureRow>
  );
}

export function ArtifactHistoryList({
  sessionId,
  index,
  scope,
  onScopeChange,
  onOpen,
}: {
  sessionId: string | null;
  index: ArtifactIndexEntry[];
  scope: ArtifactScope;
  onScopeChange: (scope: ArtifactScope) => void;
  onOpen: (entry: ArtifactIndexEntry, versionId: string) => void;
}) {
  const translations = useTranslations("ChatPanel");
  const sortedEntries = [...index].sort((left, right) => (right.updatedAt || "").localeCompare(left.updatedAt || ""));

  return (
    <Flex direction="column" flex={1} minH={0}>
      <Flex align="center" gap={2} px={4} py={2} flexShrink={0}>
        <SegmentedToggle<ArtifactScope>
          value={scope}
          onChange={onScopeChange}
          options={[
            { value: "session", label: translations("thisSession") },
            { value: "full", label: translations("allSessions") },
          ]}
        />
      </Flex>
      {sortedEntries.length === 0 ? (
        <PanelEmptyState
          icon={<LuHistory />}
          title={translations("noChangedFilesTitle")}
          description={translations("noChangedFilesDescription")}
        />
      ) : (
        <PanelBody px={4}>
          <VStack gap={1} align="stretch">
            {sessionId ? sortedEntries.map((entry) => {
              const entryKey = entry.artifactId || `${entry.gitDirectory}::${entry.relativePath}`;
              return (
                <ArtifactHistoryRow
                  key={`${entryKey}:${scope}`}
                  entry={entry}
                  sessionId={sessionId}
                  scope={scope}
                  onOpen={onOpen}
                />
              );
            }) : null}
          </VStack>
        </PanelBody>
      )}
    </Flex>
  );
}
