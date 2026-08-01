"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { Tooltip } from "./ui/tooltip";
import type { Location } from "@/lib/api";

export function locationTargetLabel(location: Pick<Location, "base_directory" | "name">): string {
  const normalizedDirectory = location.base_directory.replace(/\/+$/, "");
  return normalizedDirectory.split("/").pop() || location.name || location.base_directory;
}


// A workspace's name, which is the names of everything in it.
//
// It used to be `locations[0]`, so a workspace spanning a checkout here and a container over
// SSH was called after whichever happened to be created first — and two workspaces that shared
// that first environment were indistinguishable in the sidebar, which is the one place you pick
// between them. A workspace *is* its environments; naming it after one of them hides the rest.
//
// Joined with `Intl.ListFormat` rather than `", "`, because the separator is language: English
// separates with a comma and Japanese with `、`, which carries no space around it.
//
// `conjunction` + `narrow` after checking all six combinations, and the pairing is not the
// obvious one. `unit` reads like the right type — these are items, not alternatives — but its
// Japanese output separates with a space and nothing else, so three environments render as one
// run of words. `conjunction` at `narrow` width drops the "and"/「と」 that would claim a
// relationship a workspace does not imply, and keeps the separator each language actually uses:
// `frank, colima, scratch` and `frank、colima、scratch`.
export function workspaceLabel(
  locations: Pick<Location, "base_directory" | "name">[] | undefined,
  locale: string,
  fallback: string,
): string {
  const names = (locations ?? []).map(locationTargetLabel).filter(Boolean);
  if (names.length === 0) return fallback;
  try {
    return new Intl.ListFormat(locale, { style: "narrow", type: "conjunction" }).format(names);
  } catch {
    // A locale the platform does not know: the names still matter more than the separator.
    return names.join(", ");
  }
}

export function locationTargetAddress(location: Pick<Location, "base_directory" | "uri">): string {
  return location.uri || location.base_directory;
}

// The connection status of a location as a dot colour: local is always available (green);
// a remote is blue when its SSH host alias resolves in ~/.ssh/config, orange when it doesn't
// (so the location can't be reached).
export function locationStatusColor(location: Pick<Location, "kind" | "host_known">): string {
  if (location.kind === "local") return "green.solid";
  if (!location.host_known) return "orange.solid";
  return "blue.solid";
}

// Just the status dot — for compact inline target listings.
export function LocationStatusDot({ location }: { location: Location }) {
  return <Box boxSize="2" borderRadius="full" flexShrink={0} bg={locationStatusColor(location)} />;
}

// A full location chip: status dot + name, with the location's address (URI) revealed in a
// hover card. No "available/configured" state text — the dot colour already carries status,
// and the redundant host alias is folded into the address rather than shown as a second label.
export function LocationChip({ location }: { location: Location }) {
  const translation = useTranslations("LocationStatus");
  const color = locationStatusColor(location);
  const label = locationTargetLabel(location);
  const address = locationTargetAddress(location);
  const hostMissing = location.kind === "remote" && !location.host_known;
  const tooltipContent = (
    <Box fontSize="xs" lineHeight="1.6" maxW={80}>
      <Text fontWeight="semibold" color="fg" mb={address ? 1 : 0}>{label}</Text>
      {address && (
        <Flex align="baseline" gap={2}>
          <Text fontWeight="medium" color="fg.subtle" flexShrink={0}>{translation("uri")}</Text>
          <Text color="fg.muted" fontFamily="mono" wordBreak="break-all">{address}</Text>
        </Flex>
      )}
      {hostMissing && <Text color="orange.fg" mt={1}>{translation("hostNotFound")}</Text>}
    </Box>
  );
  return (
    <Tooltip
      content={tooltipContent}
      contentProps={{ p: 2.5, bg: "bg", color: "fg", borderRadius: "md", boxShadow: "lg", border: "1px solid", borderColor: "border" }}
      openDelay={200}
      closeDelay={60}
      positioning={{ placement: "top" }}
    >
      <Flex align="center" gap={1.5} px={2} py={1} borderRadius="sm" borderWidth="1px" borderColor="border" bg="bg">
        <Box boxSize="2" borderRadius="full" flexShrink={0} bg={color} />
        <Text textStyle="fieldLabel">{label}</Text>
      </Flex>
    </Tooltip>
  );
}
