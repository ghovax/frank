"use client";

import { Box, Button, createListCollection, Flex, Portal, Select, Span, Text } from "@chakra-ui/react";
import { useMemo, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { LuBadgeCheck, LuBox, LuCircleSlash, LuEye, LuGitBranch, LuGitFork, LuGlobe, LuHand, LuHardDrive, LuMic, LuMousePointerClick, LuUser, LuUserSearch, LuZap } from "react-icons/lu";
import type { PermissionMode } from "@/lib/api";

export type WorktreeStrategyValue = "none" | "branch" | "worktree";

// One house control size (xs / 32px, owned by the theme recipe). `layout` only decides
// whether the control hugs its content (a chip) or fills its field column.
//
// `justifyContent` is part of that decision and has to be. Chakra's select trigger is
// `space-between`, which is invisible on a chip — the content defines the width, so there is no
// free space to distribute — and wrong the moment the control fills a column: the spare width
// goes *between* the icon and the label, so a single control reads as two unrelated things
// sitting at opposite ends of a box. A field packs to the start; the dropdown indicator is
// absolutely positioned at the end either way, so nothing else moves.
function controlMetrics(layout: "chip" | "field") {
  const base = {
    borderRadius: "md" as const,
    fontSize: "xs",
    paddingX: 2,
    paddingEnd: 7,
    gap: 1.5,
    labelMaximumWidth: "none",
    contentFontSize: "xs",
    dropdownTitleFontSize: "xs",
    dropdownDescriptionFontSize: "2xs",
  };
  return layout === "field"
    ? { ...base, width: "100%", labelMaximumWidth: "100%", justifyContent: "flex-start" as const }
    : { ...base, width: "max-content", justifyContent: "space-between" as const };
}

function permissionAppearance(permissionMode: PermissionMode) {
  return {
    default: {
      icon: <LuHand size={13} />,
      color: "fg.subtle",
      background: "bg",
      borderColor: "border",
      colorPalette: undefined,
    },
    auto: {
      icon: <LuBadgeCheck size={13} />,
      color: "blue.fg",
      background: "blue.subtle",
      borderColor: "blue.muted",
      colorPalette: "blue",
    },
    read_only: {
      icon: <LuEye size={13} />,
      color: "green.fg",
      background: "green.subtle",
      borderColor: "green.muted",
      colorPalette: "green",
    },
  }[permissionMode] ?? {
    icon: <LuHand size={13} />,
    color: "fg.subtle",
    background: "bg",
    borderColor: "border",
    colorPalette: undefined,
  };
}

function worktreeAppearance(worktreeStrategy: WorktreeStrategyValue) {
  return {
    none: { icon: <LuHardDrive size={13} />, color: "fg.subtle", background: "bg", borderColor: "border", colorPalette: undefined },
    branch: { icon: <LuGitBranch size={13} />, color: "purple.fg", background: "purple.subtle", borderColor: "purple.muted", colorPalette: "purple" },
    worktree: { icon: <LuGitFork size={13} />, color: "teal.fg", background: "teal.subtle", borderColor: "teal.muted", colorPalette: "teal" },
  }[worktreeStrategy];
}

// Which agent profile runs. One control for every place that choice is made — the composer,
// where it picks what the next turn runs as, and a schedule, where it picks what fires
// unattended. Each item carries the profile's own description, because "code-investigator"
// and "senior-researcher" are not names anybody can rank without one.
export function AgentSelectControl({
  agents,
  value,
  onChange,
  layout = "chip",
  placeholder,
  responsiveCompact = false,
}: {
  agents: { id: string; name: string; title?: string; description?: string }[];
  value: string;
  onChange: (agent: string) => void;
  layout?: "chip" | "field";
  placeholder?: string;
  responsiveCompact?: boolean;
}) {
  const translation = useTranslations("SessionControls");
  const metrics = controlMetrics(layout);
  const collection = useMemo(
    () => createListCollection({
      items: agents.map((agent) => ({ label: agent.title || agent.name, value: agent.id })),
    }),
    [agents]
  );
  return (
    <Select.Root
      data-composer-agent-control={responsiveCompact ? "" : undefined}
      collection={collection}
      value={value ? [value] : []}
      onValueChange={(details) => {
        if (details.value[0]) onChange(details.value[0]);
      }}
      size="xs"
      w={metrics.width}
      minW={layout === "field" ? 0 : "max-content"}
      maxW="none"
      flexShrink={0}
    >
      <Select.Control data-composer-agent-control={responsiveCompact ? "" : undefined} w={metrics.width} minW={layout === "field" ? 0 : "max-content"} maxW="none">
        <Select.Trigger
          data-composer-agent-control={responsiveCompact ? "" : undefined}
          w={metrics.width}
          borderRadius={metrics.borderRadius}
          fontSize={metrics.fontSize}
          alignItems="center"
          justifyContent={metrics.justifyContent}
          gap={metrics.gap}
          px={metrics.paddingX}
          pe={metrics.paddingEnd}
          bg="bg"
          border="1px solid"
          borderColor="border"
          minW={layout === "field" ? 0 : "max-content"}
          maxW="none"
          whiteSpace="nowrap"
          fontWeight="medium"
          lineHeight="1"
        >
          <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" color="fg.muted" flexShrink={0}>
            <LuUser size={13} />
          </Box>
          <Select.ValueText
            data-composer-agent-label={responsiveCompact ? "" : undefined}
            placeholder={placeholder ?? translation("agentPlaceholder")}
            fontSize={metrics.contentFontSize}
            lineHeight="1"
            maxW={metrics.labelMaximumWidth}
            overflow={metrics.labelMaximumWidth === "none" ? "visible" : "hidden"}
            textOverflow={metrics.labelMaximumWidth === "none" ? "clip" : "ellipsis"}
            whiteSpace="nowrap"
          />
        </Select.Trigger>
        <Select.IndicatorGroup>
          <Select.Indicator />
        </Select.IndicatorGroup>
      </Select.Control>
      <Portal>
        <Select.Positioner>
          <Select.Content minW="220px" maxW="320px">
            {collection.items.map((item) => {
              // Look the description up from the source list by id — the collection item only
              // reliably carries label/value, so extra fields are read from `agents`.
              const description = agents.find((agent) => agent.id === item.value)?.description;
              return (
                <Select.Item item={item} key={item.value}>
                  <Flex direction="column" minW={0} flex={1}>
                    <Text fontSize={metrics.dropdownTitleFontSize} fontWeight="medium" lineHeight="1.2" whiteSpace="nowrap">{item.label}</Text>
                    {description ? (
                      <Text fontSize={metrics.dropdownDescriptionFontSize} color="fg.muted" lineHeight="1.35" truncate>{description}</Text>
                    ) : null}
                  </Flex>
                  <Select.ItemIndicator />
                </Select.Item>
              );
            })}
          </Select.Content>
        </Select.Positioner>
      </Portal>
    </Select.Root>
  );
}

// The permission mode a session runs under. Chosen before a session exists and adjustable
// afterwards — the mode is a live property of the session, not a fact settled at creation —
// so this control is the same picker in both cases.
export function PermissionModeControl({
  value,
  onChange,
  layout = "chip",
  responsiveCompact = false,
}: {
  value: PermissionMode;
  onChange: (mode: PermissionMode) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
  responsiveCompact?: boolean;
}) {
  const translation = useTranslations("SessionControls");
  const permissionChoices: { value: PermissionMode; label: string; description: string; icon: ReactNode; colorPalette?: "blue" | "green" }[] = [
    { value: "default", label: translation("permissionDefaultLabel"), description: translation("permissionDefaultDescription"), icon: <LuHand size={13} /> },
    { value: "auto", label: translation("permissionAutoLabel"), description: translation("permissionAutoDescription"), icon: <LuBadgeCheck size={13} />, colorPalette: "blue" },
    { value: "read_only", label: translation("permissionReadOnlyLabel"), description: translation("permissionReadOnlyDescription"), icon: <LuEye size={13} />, colorPalette: "green" },
  ];
  const permissionItems = permissionChoices.map(({ value: itemValue, label }) => ({ value: itemValue, label }));
  const metrics = controlMetrics(layout);
  const collection = createListCollection({ items: permissionItems });
  const selectedAppearance = permissionAppearance(value);
  const selectedLabel = permissionItems.find((item) => item.value === value)?.label ?? translation("permissionDefaultLabel");

  return (
    <Select.Root
      data-composer-permission-control={responsiveCompact ? "" : undefined}
      collection={collection}
      value={[value]}
      onValueChange={(details) => {
        const nextMode = details.value[0] as PermissionMode | undefined;
        if (nextMode) onChange(nextMode);
      }}
      size="xs"
      w={metrics.width}
      minW={layout === "field" ? 0 : "max-content"}
      maxW="none"
      flexShrink={0}
    >
      <Select.Control data-composer-permission-control={responsiveCompact ? "" : undefined} w={metrics.width} minW={layout === "field" ? 0 : "max-content"} maxW="none">
        <Select.Trigger
          data-composer-permission-control={responsiveCompact ? "" : undefined}
          w={metrics.width}
          borderRadius={metrics.borderRadius}
          fontSize={metrics.fontSize}
          alignItems="center"
          justifyContent={metrics.justifyContent}
          gap={metrics.gap}
          px={metrics.paddingX}
          pe={metrics.paddingEnd}
          bg={selectedAppearance.background}
          border="1px solid"
          borderColor={selectedAppearance.borderColor}
          colorPalette={selectedAppearance.colorPalette}
          minW={layout === "field" ? 0 : "max-content"}
          maxW="none"
          whiteSpace="nowrap"
          fontWeight="medium"
          lineHeight="1"
        >
          <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" color={selectedAppearance.color} flexShrink={0}>
            {selectedAppearance.icon}
          </Box>
          <Text data-composer-permission-label={responsiveCompact ? "" : undefined} fontSize={metrics.contentFontSize} fontWeight="medium" lineHeight="1" whiteSpace="nowrap" maxW={metrics.labelMaximumWidth} truncate={metrics.labelMaximumWidth !== "none"}>
            {selectedLabel}
          </Text>
        </Select.Trigger>
        <Select.IndicatorGroup>
          <Select.Indicator />
        </Select.IndicatorGroup>
      </Select.Control>
      <Portal>
        <Select.Positioner>
          <Select.Content minW="max-content" w="max-content">
            {collection.items.map((item) => {
              const choice = permissionChoices.find((candidate) => candidate.value === item.value);
              return (
                <Select.Item item={item} key={item.value}>
                  <Flex align="center" gap={metrics.gap} minW={0}>
                    <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" color={choice?.colorPalette ? `${choice.colorPalette}.fg` : "fg.subtle"} flexShrink={0}>
                      {choice?.icon}
                    </Box>
                    <Flex direction="column" minW={0}>
                      <Text fontSize={metrics.dropdownTitleFontSize} fontWeight="medium" lineHeight="1.2" whiteSpace="nowrap">
                        {choice?.label ?? item.label}
                      </Text>
                      {choice?.description && (
                        <Text fontSize={metrics.dropdownDescriptionFontSize} color="fg.muted" lineHeight="1.35">
                          {choice.description}
                        </Text>
                      )}
                    </Flex>
                  </Flex>
                  <Select.ItemIndicator />
                </Select.Item>
              );
            })}
          </Select.Content>
        </Select.Positioner>
      </Portal>
    </Select.Root>
  );
}

// One shared appearance shape for the toggle-style controls (sandbox, compaction,
// user-context, computer-control). Each control only computes its two-state appearance;
// the button chrome and metrics live here so all four stay pixel-identical.
interface ToggleAppearance {
  label: string;
  icon: ReactNode;
  color: string;
  background: string;
  borderColor: string;
  hover: string;
}

function ToggleControl({
  appearance,
  enabled,
  onChange,
  layout,
  labelMarker,
}: {
  appearance: ToggleAppearance;
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  layout: "chip" | "field";
  // A `data-` hook so the composer's container queries can drop this label as the row narrows,
  // exactly as they drop the agent, model and permission ones.
  labelMarker?: string;
}) {
  const metrics = controlMetrics(layout);
  return (
    <Button
      variant="outline"
      borderRadius={metrics.borderRadius}
      fontSize={metrics.fontSize}
      h={8}
      px={metrics.paddingX}
      gap={metrics.gap}
      w={metrics.width}
      justifyContent="flex-start"
      alignItems="center"
      bg={appearance.background}
      borderColor={appearance.borderColor}
      color={appearance.color}
      _hover={{ bg: appearance.hover }}
      fontWeight="medium"
      flexShrink={0}
      onClick={() => onChange?.(!enabled)}
      disabled={!onChange}
    >
      <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" flexShrink={0}>
        {appearance.icon}
      </Box>
      <Span {...(labelMarker ? { [labelMarker]: "" } : {})} fontSize={metrics.contentFontSize} fontWeight="medium" lineHeight="1">
        {appearance.label}
      </Span>
    </Button>
  );
}

// Confinement is a three-state setting in the configuration — refuse without a backend, run
// without one, or do not confine — but only two of those are a choice a person makes from a
// switch, so the switch is `required` against `off`. The paths and limits live in the
// configuration file, where a person edits them the way they edit any other Unix policy.
//
// The third state shows rather than sets: when the machine has no backend, the control says so
// instead of showing green, because a switch claiming protection that cannot be enforced is the
// exact defect this whole mechanism was built to remove.
export function SandboxToggleControl({
  enforce,
  backend,
  onChange,
  layout = "chip",
  responsiveCompact = false,
}: {
  enforce: "required" | "preferred" | "off";
  backend?: string;
  onChange?: (enforce: "required" | "preferred" | "off") => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
  responsiveCompact?: boolean;
}) {
  const translation = useTranslations("SessionControls");
  const confining = enforce !== "off";
  const enforceable = backend !== "";
  const appearance: ToggleAppearance = !confining
    ? { label: translation("sandboxGlobal"), icon: <LuGlobe size={13} />, color: "red.fg", background: "red.subtle", borderColor: "red.muted", hover: "red.muted" }
    : enforceable
      ? { label: translation("sandboxRestricted"), icon: <LuBox size={13} />, color: "green.fg", background: "green.subtle", borderColor: "green.muted", hover: "green.muted" }
      : { label: translation("sandboxUnavailable"), icon: <LuGlobe size={13} />, color: "orange.fg", background: "orange.subtle", borderColor: "orange.muted", hover: "orange.muted" };
  return (
    <ToggleControl
      appearance={appearance}
      enabled={confining}
      onChange={onChange ? (next) => onChange(next ? "required" : "off") : undefined}
      layout={layout}
      labelMarker={responsiveCompact ? "data-composer-sandbox-label" : undefined}
    />
  );
}

export function CompactionToggleControl({
  enabled,
  onChange,
  layout = "chip",
}: {
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
}) {
  const translation = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: translation("compactionAutomatic"), icon: <LuZap size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: translation("compactionManual"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
  return <ToggleControl appearance={appearance} enabled={enabled} onChange={onChange} layout={layout} />;
}

export function UserContextToggleControl({
  enabled,
  onChange,
  layout = "chip",
}: {
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
}) {
  const translation = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: translation("userContextOn"), icon: <LuUserSearch size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: translation("userContextOff"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
  return <ToggleControl appearance={appearance} enabled={enabled} onChange={onChange} layout={layout} />;
}

export function DictationToggleControl({
  enabled,
  onChange,
  layout = "chip",
}: {
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
}) {
  const translation = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: translation("dictationOn"), icon: <LuMic size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: translation("dictationOff"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
  return <ToggleControl appearance={appearance} enabled={enabled} onChange={onChange} layout={layout} />;
}

export function ComputerControlToggleControl({
  enabled,
  onChange,
  layout = "chip",
}: {
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
}) {
  const translation = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: translation("computerControlOn"), icon: <LuMousePointerClick size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: translation("computerControlOff"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
  return <ToggleControl appearance={appearance} enabled={enabled} onChange={onChange} layout={layout} />;
}

export function WorktreeStrategyControl({
  value,
  onChange,
  layout = "chip",
  disabled = false,
  gitWorktreeAvailable = true,
  title,
}: {
  value: WorktreeStrategyValue;
  onChange: (strategy: WorktreeStrategyValue) => void | Promise<void>;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
  disabled?: boolean;
  gitWorktreeAvailable?: boolean;
  title?: string;
}) {
  const translation = useTranslations("SessionControls");
  const worktreeChoices: { value: WorktreeStrategyValue; label: string; description: string; title: string; icon: ReactNode; colorPalette?: "purple" | "teal" }[] = [
    { value: "none", label: translation("worktreeNoneLabel"), description: translation("worktreeNoneDescription"), title: translation("worktreeNoneTitle"), icon: <LuHardDrive size={13} /> },
    { value: "branch", label: translation("worktreeBranchLabel"), description: translation("worktreeBranchDescription"), title: translation("worktreeBranchTitle"), icon: <LuGitBranch size={13} />, colorPalette: "purple" },
    { value: "worktree", label: translation("worktreeCopyLabel"), description: translation("worktreeCopyDescription"), title: translation("worktreeCopyTitle"), icon: <LuGitFork size={13} />, colorPalette: "teal" },
  ];
  const worktreeItems = worktreeChoices.map(({ value: itemValue, label }) => ({ value: itemValue, label }));
  const metrics = controlMetrics(layout);
  const collection = createListCollection({ items: worktreeItems });
  const selectedAppearance = worktreeAppearance(value);
  const selectedChoice = worktreeChoices.find((choice) => choice.value === value);

  return (
    <Select.Root
      collection={collection}
      value={[value]}
      onValueChange={(details) => {
        const nextStrategy = details.value[0] as WorktreeStrategyValue | undefined;
        if (nextStrategy) void onChange(nextStrategy);
      }}
      size="xs"
      w={metrics.width}
      minW={layout === "field" ? 0 : "max-content"}
      maxW="none"
      flexShrink={0}
    >
      <Select.Control w={metrics.width} minW={layout === "field" ? 0 : "max-content"} maxW="none">
        <Select.Trigger
          w={metrics.width}
          borderRadius={metrics.borderRadius}
          fontSize={metrics.fontSize}
          alignItems="center"
          justifyContent={metrics.justifyContent}
          gap={metrics.gap}
          px={metrics.paddingX}
          pe={metrics.paddingEnd}
          bg={selectedAppearance.background}
          border="1px solid"
          borderColor={selectedAppearance.borderColor}
          colorPalette={selectedAppearance.colorPalette}
          minW={layout === "field" ? 0 : "max-content"}
          maxW="none"
          whiteSpace="nowrap"
          fontWeight="medium"
          disabled={disabled}
          title={title ?? selectedChoice?.title ?? translation("worktreeStrategyFallbackTitle")}
          lineHeight="1"
        >
          <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" color={selectedAppearance.color} flexShrink={0}>
            {selectedAppearance.icon}
          </Box>
          <Select.ValueText fontSize={metrics.contentFontSize} lineHeight="1" maxW={metrics.labelMaximumWidth} overflow={metrics.labelMaximumWidth === "none" ? "visible" : "hidden"} textOverflow={metrics.labelMaximumWidth === "none" ? "clip" : "ellipsis"} whiteSpace="nowrap" />
        </Select.Trigger>
        <Select.IndicatorGroup>
          <Select.Indicator />
        </Select.IndicatorGroup>
      </Select.Control>
      <Portal>
        <Select.Positioner>
          <Select.Content minW="max-content" w="max-content">
            {collection.items.map((item) => {
              const gitModeUnavailable = item.value !== "none" && !gitWorktreeAvailable;
              const choice = worktreeChoices.find((candidate) => candidate.value === item.value);
              return (
                <Select.Item item={item} key={item.value} aria-disabled={gitModeUnavailable || undefined} data-disabled={gitModeUnavailable ? "" : undefined} opacity={gitModeUnavailable ? 0.4 : undefined} pointerEvents={gitModeUnavailable ? "none" : undefined}>
                  <Flex align="center" gap={metrics.gap} minW={0}>
                    <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" flexShrink={0}>
                      {choice?.icon}
                    </Box>
                    <Flex direction="column" minW={0}>
                      <Text fontSize={metrics.dropdownTitleFontSize} fontWeight="medium" lineHeight="1.2" whiteSpace="nowrap">
                        {choice?.label ?? item.label}
                      </Text>
                      {choice?.description && (
                        <Text fontSize={metrics.dropdownDescriptionFontSize} color="fg.muted" lineHeight="1.35">
                          {choice.description}
                        </Text>
                      )}
                    </Flex>
                  </Flex>
                  <Select.ItemIndicator />
                </Select.Item>
              );
            })}
          </Select.Content>
        </Select.Positioner>
      </Portal>
    </Select.Root>
  );
}
