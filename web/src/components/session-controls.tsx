"use client";

import { Box, Button, createListCollection, Flex, Portal, Select, Span, Text } from "@chakra-ui/react";
import { useMemo, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { LuBadgeCheck, LuBox, LuCheck, LuCircleSlash, LuEye, LuGitBranch, LuGitFork, LuGlobe, LuHand, LuHardDrive, LuMic, LuMousePointerClick, LuPackage, LuUser, LuUserSearch, LuZap } from "react-icons/lu";
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

// A control in a row that fits itself: what it answers to, and whether it is currently down to its
// icon. The size that goes with `data-fit-collapsed` lives in `globals.css` rather than here,
// because `useFittedRow` decides by *applying* a candidate to the DOM and measuring it — so the
// collapsed state has to be reachable by setting one attribute, or what gets measured is not what
// gets drawn. `hasArrow` says the control keeps a dropdown arrow beside the square.
function fitMarkers(id: string | undefined, labelHidden: boolean, hasArrow: boolean) {
  if (!id) return {};
  return {
    "data-fit-control": id,
    ...(hasArrow ? { "data-fit-arrow": "" } : {}),
    ...(labelHidden ? { "data-fit-collapsed": "" } : {}),
  };
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
    permissive: {
      icon: <LuZap size={13} />,
      color: "orange.fg",
      background: "orange.subtle",
      borderColor: "orange.muted",
      colorPalette: "orange",
    },
    classify: {
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
  fitted = false,
  labelHidden = false,
}: {
  agents: { id: string; name: string; title?: string; description?: string }[];
  value: string;
  onChange: (agent: string) => void;
  layout?: "chip" | "field";
  placeholder?: string;
  fitted?: boolean;
  /** The row this sits in has no space for the name; show the icon and the arrow alone. */
  labelHidden?: boolean;
}) {
  const translation = useTranslations("SessionControls");
  const metrics = controlMetrics(layout);
  const markers = fitMarkers(fitted ? "agent" : undefined, labelHidden, true);
  const collection = useMemo(
    () => createListCollection({
      items: agents.map((agent) => ({ label: agent.title || agent.name, value: agent.id })),
    }),
    [agents]
  );
  return (
    <Select.Root
      collection={collection}
      value={value ? [value] : []}
      onValueChange={(details) => {
        if (details.value[0]) onChange(details.value[0]);
      }}
      size="xs"
      {...markers}
      w={metrics.width}
      minW={layout === "field" ? 0 : "max-content"}
      maxW="none"
      flexShrink={0}
    >
      <Select.Control {...markers} w={metrics.width} minW={layout === "field" ? 0 : "max-content"} maxW="none">
        <Select.Trigger
          {...markers}
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
        >
          <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" color="fg.muted" flexShrink={0}>
            <LuUser size={13} />
          </Box>
          <Select.ValueText
            data-fit-label={fitted ? "agent" : undefined}
            data-fit-hidden={fitted && labelHidden ? "" : undefined}
            placeholder={placeholder ?? translation("agentPlaceholder")}
            fontSize={metrics.contentFontSize}
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
  fitted = false,
  labelHidden = false,
  unsetLabel,
}: {
  value: PermissionMode | null;
  onChange: (mode: PermissionMode | null) => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
  fitted?: boolean;
  /** The row this sits in has no space for the mode's name; the icon and its colour say it. */
  labelHidden?: boolean;
  /**
   * When given, the control offers this as a first choice meaning "no mode", and `onChange`
   * answers `null` for it.
   *
   * Only an agent card wants this. A card that names a mode is declaring a *ceiling* — the
   * loosest its agent may ever run at — and most cards mean to declare nothing, which a control
   * that can only emit a mode cannot express. A session picker has no use for it: a session
   * always runs under some mode.
   */
  unsetLabel?: string;
}) {
  const translation = useTranslations("SessionControls");
  const permissionChoices: { value: PermissionMode; label: string; description: string; icon: ReactNode; colorPalette?: "blue" | "green" | "orange" }[] = [
    { value: "default", label: translation("permissionDefaultLabel"), description: translation("permissionDefaultDescription"), icon: <LuHand size={13} /> },
    { value: "permissive", label: translation("permissionPermissiveLabel"), description: translation("permissionPermissiveDescription"), icon: <LuZap size={13} />, colorPalette: "orange" },
    { value: "classify", label: translation("permissionClassifyLabel"), description: translation("permissionClassifyDescription"), icon: <LuBadgeCheck size={13} />, colorPalette: "blue" },
    { value: "read_only", label: translation("permissionReadOnlyLabel"), description: translation("permissionReadOnlyDescription"), icon: <LuEye size={13} />, colorPalette: "green" },
  ];
  const UNSET = "__unset__";
  const permissionItems = [
    ...(unsetLabel ? [{ value: UNSET, label: unsetLabel }] : []),
    ...permissionChoices.map(({ value: itemValue, label }) => ({ value: itemValue, label })),
  ];
  const metrics = controlMetrics(layout);
  const markers = fitMarkers(fitted ? "permission" : undefined, labelHidden, true);
  const collection = createListCollection({ items: permissionItems });
  const selectedAppearance = permissionAppearance(value ?? "default");
  const selectedLabel = permissionItems.find((item) => item.value === (value ?? UNSET))?.label
    ?? translation("permissionDefaultLabel");

  return (
    <Select.Root
      collection={collection}
      value={[value ?? UNSET]}
      onValueChange={(details) => {
        const chosen = details.value[0];
        if (!chosen) return;
        onChange(chosen === UNSET ? null : (chosen as PermissionMode));
      }}
      size="xs"
      {...markers}
      w={metrics.width}
      minW={layout === "field" ? 0 : "max-content"}
      maxW="none"
      flexShrink={0}
    >
      <Select.Control {...markers} w={metrics.width} minW={layout === "field" ? 0 : "max-content"} maxW="none">
        <Select.Trigger
          {...markers}
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
        >
          <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" color={selectedAppearance.color} flexShrink={0}>
            {selectedAppearance.icon}
          </Box>
          <Text
            data-fit-label={fitted ? "permission" : undefined}
            data-fit-hidden={fitted && labelHidden ? "" : undefined}
            fontSize={metrics.contentFontSize}
            fontWeight="medium"
            whiteSpace="nowrap"
            maxW={metrics.labelMaximumWidth}
            truncate={metrics.labelMaximumWidth !== "none"}
          >
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
  fitId,
  labelHidden = false,
}: {
  appearance: ToggleAppearance;
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  layout: "chip" | "field";
  /** The name this toggle's label answers to when the row it is in has to shed labels. */
  fitId?: string;
  labelHidden?: boolean;
}) {
  const metrics = controlMetrics(layout);
  // No arrow: this is a button, not a picker, so with its word gone it is a plain square.
  const markers = fitMarkers(fitId, labelHidden, false);
  return (
    <Button
      {...markers}
      variant="outline"
      borderRadius={metrics.borderRadius}
      fontSize={metrics.fontSize}
      // Both dimensions from the same variable, so the square stays square on a touch device,
      // where a control is 40px rather than 32.
      h="var(--control-height)"
      px={metrics.paddingX}
      gap={metrics.gap}
      w={metrics.width}
      minW="max-content"
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
      <Span
        data-fit-label={fitId}
        data-fit-hidden={fitId && labelHidden ? "" : undefined}
        fontSize={metrics.contentFontSize}
        fontWeight="medium"
        minW={0}
        truncate
      >
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
  fitted = false,
  labelHidden = false,
}: {
  enforce: "required" | "preferred" | "off";
  backend?: string;
  onChange?: (enforce: "required" | "preferred" | "off") => void;
  size?: "xs" | "sm";
  layout?: "chip" | "field";
  fitted?: boolean;
  /** The row this sits in has no space for the word; the globe and the box carry it alone. */
  labelHidden?: boolean;
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
      fitId={fitted ? "sandbox" : undefined}
      labelHidden={labelHidden}
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

export function SettingToggleControl({
  enabled,
  onChange,
  layout = "chip",
}: {
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  layout?: "chip" | "field";
}) {
  // The toggle for a setting that has no words of its own — the generated rows, where the name
  // and the explanation are the row's and the control only has to say on or off. Every other
  // toggle in this file names its subject twice, once in each state ("Own tools" / "No own
  // tools"), which reads well beside a chip in the composer and reads as a stutter under a
  // label that just said it.
  const translation = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: translation("settingOn"), icon: <LuCheck size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: translation("settingOff"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
  return <ToggleControl appearance={appearance} enabled={enabled} onChange={onChange} layout={layout} />;
}

export function ToolboxToggleControl({
  enabled,
  onChange,
  layout = "chip",
}: {
  enabled: boolean;
  onChange?: (enabled: boolean) => void;
  layout?: "chip" | "field";
}) {
  const translation = useTranslations("SessionControls");
  const appearance: ToggleAppearance = enabled
    ? { label: translation("toolboxOn"), icon: <LuPackage size={13} />, color: "blue.fg", background: "blue.subtle", borderColor: "blue.muted", hover: "blue.muted" }
    : { label: translation("toolboxOff"), icon: <LuCircleSlash size={13} />, color: "fg.muted", background: "bg.subtle", borderColor: "border", hover: "bg.muted" };
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
        >
          <Box display="flex" alignItems="center" justifyContent="center" boxSize="3.5" color={selectedAppearance.color} flexShrink={0}>
            {selectedAppearance.icon}
          </Box>
          <Select.ValueText fontSize={metrics.contentFontSize} maxW={metrics.labelMaximumWidth} overflow={metrics.labelMaximumWidth === "none" ? "visible" : "hidden"} textOverflow={metrics.labelMaximumWidth === "none" ? "clip" : "ellipsis"} whiteSpace="nowrap" />
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
