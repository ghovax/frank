"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { locationTargetLabel, workspaceLabel } from "@shared/workspace";

import { Tooltip } from "./ui/tooltip";
import type { Location } from "@/lib/api";

// The naming lives in `@shared/workspace` because the phone names the same workspaces.
export { locationTargetLabel, workspaceLabel };

export function locationTargetAddress(location: Pick<Location, "base_directory" | "uri">): string {
  return location.uri || location.base_directory;
}

// A location's status as a dot colour: local is green, a remote is blue when its alias resolves.
export function locationStatusColor(location: Pick<Location, "kind" | "host_known">): string {
  if (location.kind === "local") return "green.solid";
  if (!location.host_known) return "orange.solid";
  return "blue.solid";
}

// Just the status dot — for compact inline target listings.
export function LocationStatusDot({ location }: { location: Location }) {
  return <Box boxSize="2" borderRadius="full" flexShrink={0} bg={locationStatusColor(location)} />;
}

// A location chip: status dot and name, with the address in a hover card.
export function LocationChip({ location }: { location: Location }) {
  const translation = useTranslations("LocationStatus");
  const color = locationStatusColor(location);
  const label = locationTargetLabel(location);
  const address = locationTargetAddress(location);
  const hostMissing = location.kind === "remote" && !location.host_known;
  const tooltipContent = (
    <Box fontSize="xs" lineHeight="1.6" maxW={80}>
      <Text fontWeight="semibold" color="fg" mb={address ? 1 : 0}>
        {label}
      </Text>
      {address && (
        <Flex align="baseline" gap={2}>
          <Text fontWeight="medium" color="fg.subtle" flexShrink={0}>
            {translation("uri")}
          </Text>
          <Text color="fg.muted" fontFamily="mono" wordBreak="break-all">
            {address}
          </Text>
        </Flex>
      )}
      {hostMissing && (
        <Text color="orange.fg" mt={1}>
          {translation("hostNotFound")}
        </Text>
      )}
    </Box>
  );
  return (
    <Tooltip
      content={tooltipContent}
      contentProps={{
        p: 2.5,
        bg: "bg",
        color: "fg",
        borderRadius: "md",
        boxShadow: "lg",
        border: "1px solid",
        borderColor: "border",
      }}
      openDelay={200}
      closeDelay={60}
      positioning={{ placement: "top" }}
    >
      <Flex
        align="center"
        gap={1.5}
        px={2}
        py={1}
        borderRadius="sm"
        borderWidth="1px"
        borderColor="border"
        bg="bg"
      >
        <Box boxSize="2" borderRadius="full" flexShrink={0} bg={color} />
        <Text textStyle="fieldLabel">{label}</Text>
      </Flex>
    </Tooltip>
  );
}
