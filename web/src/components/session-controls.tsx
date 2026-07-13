"use client";

import { Box, Button, createListCollection, Flex, Portal, Select, Text } from "@chakra-ui/react";
import { type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { LuBox, LuCircleSlash, LuEye, LuGitBranch, LuGitFork, LuGlobe, LuHardDrive, LuMousePointerClick, LuSlidersHorizontal, LuUserSearch, LuZap } from "react-icons/lu";
import type { PermissionMode } from "@/lib/api";

export type WorkspaceStrategyValue = "none" | "branch" | "worktree";

// One house control size (xs / 32px, owned by the theme recipe). `layout` only decides
// whether the control hugs its content (a chip) or fills its field column.
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
    dropdownPaddingY: 1.5,
  };
  return layout === "field" ? { ...base, width: "100%", labelMaximumWidth: "100%" } : { ...base, width: "max-content" };
}

function permissionAppearance(permissionMode: PermissionMode) {
  return {
    default: {
      icon: <LuSlidersHorizontal size={13} />,
      color: "fg.subtle",
      background: "bg",
      borderColor: "border",
      colorPalette: undefined,
    },
    auto: {
      icon: <LuZap size={13} />,
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
    bypass: {
      icon: <LuCircleSlash size={13} />,
      color: "red.fg",
      background: "red.subtle",
      borderColor: "red.muted",
      colorPalette: "red",
    },
  }[permissionMode] ?? {
    icon: <LuSlidersHorizontal size={13} />,
    color: "fg.subtle",
    background: "bg",
    borderColor: "border",
    colorPalette: undefined,
  };
}

function workspaceAppearance(workspaceStrategy: WorkspaceStrategyValue) {
  return {
    none: { icon: <LuHardDrive size={13} />, color: "fg.subtle", background: "bg", borderColor: "border", colorPalette: undefined },
    branch: { icon: <LuGitBranch size={13} />, color: "purple.fg", background: "purple.subtle", borderColor: "purple.muted", colorPalette: "purple" },
    worktree: { icon: <LuGitFork size={13} />, color: "teal.fg", background: "teal.subtle", borderColor: "teal.muted", colorPalette: "teal" },
  }[workspaceStrategy];
}

export function PermissionModeControl({
  value,
  onChange,
  layout = "chip",
}: {
  value: PermissionMode;
  onChange: (mode: PermissionMode) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
}) {
  const t = useTranslations("SessionControls");
  const permissionChoices: { value: PermissionMode; label: string; description: string; icon: ReactNode; colorPalette?: "blue" | "green" | "red" }[] = [
    { value: "default", label: t("permissionDefaultLabel"), description: t("permissionDefaultDescription"), icon: <LuSlidersHorizontal size={13} /> },
    { value: "auto", label: t("permissionAutoLabel"), description: t("permissionAutoDescription"), icon: <LuZap size={13} />, colorPalette: "blue" },
    { value: "read_only", label: t("permissionReadOnlyLabel"), description: t("permissionReadOnlyDescription"), icon: <LuEye size={13} />, colorPalette: "green" },
    { value: "bypass", label: t("permissionBypassLabel"), description: t("permissionBypassDescription"), icon: <LuCircleSlash size={13} />, colorPalette: "red" },
  ];
  const permissionItems = permissionChoices.map(({ value: itemValue, label }) => ({ value: itemValue, label }));
  const metrics = controlMetrics(layout);
  const collection = createListCollection({ items: permissionItems });
  const selectedAppearance = permissionAppearance(value);
  const selectedLabel = permissionItems.find((item) => item.value === value)?.label ?? t("permissionDefaultLabel");

  return (
    <Select.Root
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
      <Select.Control w={metrics.width} minW={layout === "field" ? 0 : "max-content"} maxW="none">
        <Select.Trigger
          w={metrics.width}
          borderRadius={metrics.borderRadius}
          fontSize={metrics.fontSize}
          alignItems="center"
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
          <Text fontSize={metrics.contentFontSize} fontWeight="medium" lineHeight="1" whiteSpace="nowrap" maxW={metrics.labelMaximumWidth} truncate={metrics.labelMaximumWidth !== "none"}>
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
                <Select.Item item={item} key={item.value} fontWeight="medium" fontSize={metrics.dropdownTitleFontSize} py={metrics.dropdownPaddingY}>
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
}: {
  appearance: ToggleAppearance;
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  layout: "chip" | "field";
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
      <Text as="span" fontSize={metrics.contentFontSize} fontWeight="medium" lineHeight="1">
        {appearance.label}
      </Text>
    </Button>
  );
}

export function SandboxToggleControl({
  enabled,
  onChange,
  layout = "chip",
}: {
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
}) {
  const t = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: t("sandboxRestricted"), icon: <LuBox size={13} />, color: "green.fg", background: "green.subtle", borderColor: "green.muted", hover: "green.muted" }
    : { label: t("sandboxGlobal"), icon: <LuGlobe size={13} />, color: "red.fg", background: "red.subtle", borderColor: "red.muted", hover: "red.muted" };
  return <ToggleControl appearance={appearance} enabled={enabled} onChange={onChange} layout={layout} />;
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
  const t = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: t("compactionAutomatic"), icon: <LuZap size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: t("compactionManual"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
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
  const t = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: t("userContextOn"), icon: <LuUserSearch size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: t("userContextOff"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
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
  const t = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: t("computerControlOn"), icon: <LuMousePointerClick size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: t("computerControlOff"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
  return <ToggleControl appearance={appearance} enabled={enabled} onChange={onChange} layout={layout} />;
}

export function WorkspaceStrategyControl({
  value,
  onChange,
  layout = "chip",
  disabled = false,
  gitWorkspaceAvailable = true,
  title,
}: {
  value: WorkspaceStrategyValue;
  onChange: (strategy: WorkspaceStrategyValue) => void | Promise<void>;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
  disabled?: boolean;
  gitWorkspaceAvailable?: boolean;
  title?: string;
}) {
  const t = useTranslations("SessionControls");
  const workspaceChoices: { value: WorkspaceStrategyValue; label: string; description: string; title: string; icon: ReactNode; colorPalette?: "purple" | "teal" }[] = [
    { value: "none", label: t("workspaceNoneLabel"), description: t("workspaceNoneDescription"), title: t("workspaceNoneTitle"), icon: <LuHardDrive size={13} /> },
    { value: "branch", label: t("workspaceBranchLabel"), description: t("workspaceBranchDescription"), title: t("workspaceBranchTitle"), icon: <LuGitBranch size={13} />, colorPalette: "purple" },
    { value: "worktree", label: t("workspaceWorktreeLabel"), description: t("workspaceWorktreeDescription"), title: t("workspaceWorktreeTitle"), icon: <LuGitFork size={13} />, colorPalette: "teal" },
  ];
  const workspaceItems = workspaceChoices.map(({ value: itemValue, label }) => ({ value: itemValue, label }));
  const metrics = controlMetrics(layout);
  const collection = createListCollection({ items: workspaceItems });
  const selectedAppearance = workspaceAppearance(value);
  const selectedChoice = workspaceChoices.find((choice) => choice.value === value);

  return (
    <Select.Root
      collection={collection}
      value={[value]}
      onValueChange={(details) => {
        const nextStrategy = details.value[0] as WorkspaceStrategyValue | undefined;
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
          title={title ?? selectedChoice?.title ?? t("workspaceStrategyFallbackTitle")}
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
              const gitModeUnavailable = item.value !== "none" && !gitWorkspaceAvailable;
              const choice = workspaceChoices.find((candidate) => candidate.value === item.value);
              return (
                <Select.Item item={item} key={item.value} fontWeight="medium" fontSize={metrics.dropdownTitleFontSize} py={metrics.dropdownPaddingY} aria-disabled={gitModeUnavailable || undefined} data-disabled={gitModeUnavailable ? "" : undefined} opacity={gitModeUnavailable ? 0.4 : undefined} pointerEvents={gitModeUnavailable ? "none" : undefined}>
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
