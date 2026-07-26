"use client";

import { Alert, Box, Button, Dialog, EmptyState, Flex, IconButton, Input, Portal, Spinner, Text, VStack } from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { LuEye, LuEyeOff, LuKeyRound, LuPlug, LuPlus, LuSearch, LuServer, LuTrash2, LuUsers } from "react-icons/lu";
import { fetchAccessibility, fetchAgentConfiguration, fetchFullDiskAccess, fetchSettings, openAccessibilitySettings, openFullDiskAccessSettings, restartApp, restartDaemon, saveAgentConfiguration, saveSettings, subscribeEvents, updateCompactionSettings, updateComputerControlSetting, updateUserContextSetting, type AgentConfiguration, type AgentSummary, type ModelOption, type PermissionMode, type ProviderOption, type RecentModel, type SandboxEnforce } from "@/lib/api";
import type { ConnectionTarget } from "@/lib/connection";
import { ConnectionSettings } from "./connection-settings";
import { ConnectionSwitcher } from "./connection-switcher";
import { ModelSelect } from "./model-select";
import { ChatGPTAuthControl } from "./chatgpt-auth";
import { CursorAuthControl } from "./cursor-auth";
import { ProjectLocationsPanel } from "./project-locations";
import { RemoteAgentsPanel } from "./remote-agents-panel";
import { SimpleSelect } from "./ui/simple-select";
import { useColorMode } from "./ui/color-mode";
import { ConfirmDialog } from "./ui/confirm-dialog";
import { useTranslations } from "next-intl";
import { useLocale } from "@/lib/i18n/locale-provider";
import { LOCALES, type Locale } from "@/lib/i18n/messages";
import { CompactionToggleControl, ComputerControlToggleControl, PermissionModeControl, SandboxToggleControl, UserContextToggleControl, WorkspaceStrategyControl, type WorkspaceStrategyValue } from "./session-controls";
import { useScrollEdgeFade } from "@/lib/scroll-fade";
import { Section } from "./ui/semantic";

export type SettingsSection = "general" | "locations" | "agents" | "connection";

// One setting modeled as data (so the search box can match it): a title + optional
// description that renders on the left, and a `control` that renders on the right. `stacked`
// puts a wide control (an API-key field) on its own line beneath the label instead.
type SettingRowDef = { key: string; title: string; description?: string; control: ReactNode; layout?: "row" | "stacked" };
type SettingsPageSection = { title?: string; rows: SettingRowDef[]; block?: ReactNode };
type SettingsPage = { id: SettingsSection; label: string; icon: ReactNode; sections: SettingsPageSection[] };

// A titled section: a heading over a stack of rows separated by hairline dividers (the
// trailing full-width block, if any, sits beneath the rows).
function SettingsSection({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <Section mb={8} _last={{ mb: 0 }}>
      {title ? <Text fontSize="sm" fontWeight="semibold" color="fg" mb={2}>{title}</Text> : null}
      <Box css={{ "& > *": { borderColor: "var(--chakra-colors-border-muted)" }, "& > *:not(:last-child)": { borderBottomWidth: "1px" } }}>
        {children}
      </Box>
    </Section>
  );
}

// One settings row: label (+ description) on the left, control on the right. A `stacked`
// row drops the control to its own line for wide inputs.
function SettingRow({ title, description, children, layout = "row" }: { title: string; description?: string; children: ReactNode; layout?: "row" | "stacked" }) {
  const stacked = layout === "stacked";
  return (
    <Flex
      direction={{ base: "column", sm: stacked ? "column" : "row" }}
      align={{ base: "stretch", sm: stacked ? "stretch" : "center" }}
      justify="space-between"
      gap={{ base: 2, sm: stacked ? 2 : 6 }}
      py={3}
    >
      <Box minW={0} flex={stacked ? undefined : 1}>
        <Text textStyle="fieldLabel" color="fg">{title}</Text>
        {description ? <Text fontSize="xs" color="fg.muted" mt={0.5}>{description}</Text> : null}
      </Box>
      <Box flexShrink={{ base: 1, sm: 0 }} alignSelf={{ base: "stretch", sm: stacked ? "stretch" : undefined }}>{children}</Box>
    </Flex>
  );
}

// A dialog for entering API credentials and configuring runtime/agent behavior.
// Saving applies everything live (no restart).
export function SettingsDialog({
  open,
  onOpenChange,
  section,
  onSectionChange,
  currentConnectionId,
  onConnectionChange,
  projectId = "",
  workingDirectory = "",
  models = [],
  modelProviders = [],
  recentModels = [],
  agents = [],
  selectedAgent = "",
  onAgentChange,
  livePermissionMode = "default",
  liveSandboxEnforce = "required" as SandboxEnforce,
  sandboxBackend = { backend: "", detail: "" },
  liveWorkspaceStrategy = "none",
  onPermissionModeChange,
  onSandboxEnforceChange,
  onWorkspaceStrategyChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  currentConnectionId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
  projectId?: string;
  workingDirectory?: string;
  models?: ModelOption[];
  modelProviders?: ProviderOption[];
  recentModels?: RecentModel[];
  agents?: AgentSummary[];
  selectedAgent?: string;
  onAgentChange?: (agent: string) => void;
  livePermissionMode?: PermissionMode;
  liveSandboxEnforce?: SandboxEnforce;
  sandboxBackend?: { backend: string; detail: string };
  liveWorkspaceStrategy?: WorkspaceStrategyValue;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  onSandboxEnforceChange?: (enforce: SandboxEnforce) => void | Promise<void>;
  onWorkspaceStrategyChange?: (strategy: WorkspaceStrategyValue) => void | Promise<void>;
}) {
  const translation = useTranslations("SettingsDialog");
  const tc = useTranslations("Common");
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(livePermissionMode);
  const [savedPermissionMode, setSavedPermissionMode] = useState<PermissionMode>(livePermissionMode);
  const [sandboxEnforce, setSandboxEnforce] = useState<SandboxEnforce>(liveSandboxEnforce);
  const [savedSandboxEnforce, setSavedSandboxEnforce] = useState<SandboxEnforce>(liveSandboxEnforce);
  const [workspaceStrategy, setWorkspaceStrategy] = useState<WorkspaceStrategyValue>(liveWorkspaceStrategy);
  const [savedWorkspaceStrategy, setSavedWorkspaceStrategy] = useState<WorkspaceStrategyValue>(liveWorkspaceStrategy);
  const [autoCompaction, setAutoCompaction] = useState(false);
  const [savedAutoCompaction, setSavedAutoCompaction] = useState(false);
  const [userContextEnabled, setUserContextEnabled] = useState(false);
  const [savedUserContextEnabled, setSavedUserContextEnabled] = useState(false);
  const [computerControlEnabled, setComputerControlEnabled] = useState(false);
  const [savedComputerControlEnabled, setSavedComputerControlEnabled] = useState(false);
  // Whether the server can read Full-Disk-Access-protected data (Screen Time, Safari history).
  // `null` while unknown; drives the banner shown when user-context is on but FDA is missing.
  const [fullDiskAccess, setFullDiskAccess] = useState<boolean | null>(null);
  const [accessibilityGranted, setAccessibilityGranted] = useState<boolean | null>(null);
  // The grant flow: after "Grant access" opens System Settings, returning focus to the app
  // offers a one-click restart (macOS only reflects the grant to the server on a fresh launch).
  const [awaitingGrantReturn, setAwaitingGrantReturn] = useState(false);
  const [restartPromptOpen, setRestartPromptOpen] = useState(false);
  const [exaApiKey, setExaApiKey] = useState("");
  const [savedExaApiKey, setSavedExaApiKey] = useState("");
  const [composioApiKey, setComposioApiKey] = useState("");
  const [savedComposioApiKey, setSavedComposioApiKey] = useState("");
  const [jinaApiKey, setJinaApiKey] = useState("");
  const [savedJinaApiKey, setSavedJinaApiKey] = useState("");
  const [firecrawlApiKey, setFirecrawlApiKey] = useState("");
  const [savedFirecrawlApiKey, setSavedFirecrawlApiKey] = useState("");
  const [webFetchProxyUrl, setWebFetchProxyUrl] = useState("");
  const [savedWebFetchProxyUrl, setSavedWebFetchProxyUrl] = useState("");
  const [connectionDirty, setConnectionDirty] = useState(false);
  const [connectionResetToken, setConnectionResetToken] = useState(0);
  const [settingsAgent, setSettingsAgent] = useState(selectedAgent);
  const [agentConfiguration, setAgentConfiguration] = useState<AgentConfiguration | null>(null);
  const [savedAgentConfiguration, setSavedAgentConfiguration] = useState<AgentConfiguration | null>(null);
  const [agentError, setAgentError] = useState("");
  // The agent+folder the config was last resolved for. `agentLoading` is derived from
  // it rather than set synchronously in the fetch effect, so loading is simply "the
  // request in flight hasn't landed yet" — no cascading setState inside the effect.
  const [loadedAgentKey, setLoadedAgentKey] = useState("");
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const agentDirty = JSON.stringify(agentConfiguration) !== JSON.stringify(savedAgentConfiguration);
  const generalDirty =
    exaApiKey !== savedExaApiKey
    || composioApiKey !== savedComposioApiKey
    || jinaApiKey !== savedJinaApiKey
    || firecrawlApiKey !== savedFirecrawlApiKey
    || webFetchProxyUrl !== savedWebFetchProxyUrl
    || permissionMode !== savedPermissionMode
    || sandboxEnforce !== savedSandboxEnforce
    || workspaceStrategy !== savedWorkspaceStrategy
    || autoCompaction !== savedAutoCompaction
    || userContextEnabled !== savedUserContextEnabled
    || computerControlEnabled !== savedComputerControlEnabled;
  const hasUnsavedChanges = generalDirty || agentDirty || connectionDirty;
  const agentItems = useMemo(
    () => agents.map((agent) => ({ label: agent.title || agent.name, value: agent.id })),
    [agents]
  );

  // Pre-fill from the server each time the dialog opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchSettings()
      .then((settings) => {
        if (cancelled) return;
        setPermissionMode(settings.permission_mode ?? "default");
        setSavedPermissionMode(settings.permission_mode ?? "default");
        setSandboxEnforce(settings.sandbox?.enforce ?? "required");
        setSavedSandboxEnforce(settings.sandbox?.enforce ?? "required");
        setWorkspaceStrategy(settings.workspace_strategy ?? "none");
        setSavedWorkspaceStrategy(settings.workspace_strategy ?? "none");
        setAutoCompaction(settings.compaction?.auto ?? false);
        setSavedAutoCompaction(settings.compaction?.auto ?? false);
        setUserContextEnabled(settings.user_context_enabled ?? false);
        setSavedUserContextEnabled(settings.user_context_enabled ?? false);
        setComputerControlEnabled(settings.computer_control_enabled ?? false);
        setSavedComputerControlEnabled(settings.computer_control_enabled ?? false);
        setExaApiKey(settings.exa_api_key ?? "");
        setSavedExaApiKey(settings.exa_api_key ?? "");
        setComposioApiKey(settings.composio_api_key ?? "");
        setSavedComposioApiKey(settings.composio_api_key ?? "");
        setJinaApiKey(settings.jina_api_key ?? "");
        setSavedJinaApiKey(settings.jina_api_key ?? "");
        setFirecrawlApiKey(settings.firecrawl_api_key ?? "");
        setSavedFirecrawlApiKey(settings.firecrawl_api_key ?? "");
        setWebFetchProxyUrl(settings.web_fetch_proxy_url ?? "");
        setSavedWebFetchProxyUrl(settings.web_fetch_proxy_url ?? "");
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Check Full Disk Access only when it's relevant (user-context on), and re-check whenever
  // the window regains focus — so returning from the System Settings pane refreshes the banner
  // without a manual step.
  useEffect(() => {
    // Only relevant while the dialog is open with user-context on; the banner is gated on
    // `userContextEnabled` too, so no reset is needed when it's off (avoids a sync setState).
    if (!open || !userContextEnabled) return;
    let cancelled = false;
    const check = () => { void fetchFullDiskAccess().then((granted) => { if (!cancelled) setFullDiskAccess(granted); }); };
    check();
    window.addEventListener("focus", check);
    return () => { cancelled = true; window.removeEventListener("focus", check); };
  }, [open, userContextEnabled]);

  // Accessibility powers the computer-use tool, and must be granted *before* the feature can be
  // enabled — so it is checked whenever the dialog is open (not gated on the toggle), the grant
  // banner shows by default while it is missing, and the toggle stays disabled until it is
  // present. Re-checked on window focus so returning from the System Settings pane updates live.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const check = () => { void fetchAccessibility().then((granted) => { if (!cancelled) setAccessibilityGranted(granted); }); };
    check();
    window.addEventListener("focus", check);
    return () => { cancelled = true; window.removeEventListener("focus", check); };
  }, [open]);

  // After "Grant access" sends the user to System Settings, re-check accessibility whenever focus
  // returns and only offer the restart once it is actually detected as granted — the permission
  // then takes effect on a fresh launch of the server. Returning without granting (just looking,
  // or toggling something else) must NOT pop the restart dialog.
  useEffect(() => {
    if (!awaitingGrantReturn) return;
    const onFocus = () => {
      void fetchAccessibility().then((granted) => {
        setAccessibilityGranted(granted);
        if (granted) {
          setAwaitingGrantReturn(false);
          setRestartPromptOpen(true);
        }
      });
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [awaitingGrantReturn]);

  // Latest field values, mirrored into a ref so the live-sync subscription below can
  // read them without re-subscribing on every keystroke.
  const fieldsRef = useRef({
    permissionMode, savedPermissionMode, sandboxEnforce, savedSandboxEnforce,
    workspaceStrategy, savedWorkspaceStrategy, autoCompaction, savedAutoCompaction,
    userContextEnabled, savedUserContextEnabled,
    computerControlEnabled, savedComputerControlEnabled,
    exaApiKey, savedExaApiKey, composioApiKey, savedComposioApiKey,
    jinaApiKey, savedJinaApiKey, firecrawlApiKey, savedFirecrawlApiKey,
    webFetchProxyUrl, savedWebFetchProxyUrl,
  });
  useEffect(() => {
    fieldsRef.current = {
      permissionMode, savedPermissionMode, sandboxEnforce, savedSandboxEnforce,
      workspaceStrategy, savedWorkspaceStrategy, autoCompaction, savedAutoCompaction,
      userContextEnabled, savedUserContextEnabled,
      computerControlEnabled, savedComputerControlEnabled,
      exaApiKey, savedExaApiKey, composioApiKey, savedComposioApiKey,
    jinaApiKey, savedJinaApiKey, firecrawlApiKey, savedFirecrawlApiKey,
    webFetchProxyUrl, savedWebFetchProxyUrl,
    };
  });

  // Live sync while the dialog is open: when the configuration changes elsewhere (a
  // manual edit to the file, another window, or the server), move each field's saved
  // baseline to the new truth and update the visible value ONLY for fields the user has
  // not touched — so an external change appears in real time without clobbering an
  // in-progress edit. (`settings_changed` is debounced+deduplicated on the server.)
  useEffect(() => {
    if (!open) return;
    return subscribeEvents((event) => {
      if (event.type !== "settings_changed") return;
      fetchSettings()
        .then((settings) => {
          const fields = fieldsRef.current;
          const reconcile = <T,>(next: T, current: T, saved: T, setCurrent: (value: T) => void, setSaved: (value: T) => void) => {
            setSaved(next);
            if (current === saved) setCurrent(next);
          };
          reconcile(settings.permission_mode ?? "default", fields.permissionMode, fields.savedPermissionMode, setPermissionMode, setSavedPermissionMode);
          reconcile(settings.sandbox?.enforce ?? "required", fields.sandboxEnforce, fields.savedSandboxEnforce, setSandboxEnforce, setSavedSandboxEnforce);
          reconcile((settings.workspace_strategy ?? "none") as WorkspaceStrategyValue, fields.workspaceStrategy, fields.savedWorkspaceStrategy, setWorkspaceStrategy, setSavedWorkspaceStrategy);
          reconcile(settings.compaction?.auto ?? false, fields.autoCompaction, fields.savedAutoCompaction, setAutoCompaction, setSavedAutoCompaction);
          reconcile(settings.user_context_enabled ?? false, fields.userContextEnabled, fields.savedUserContextEnabled, setUserContextEnabled, setSavedUserContextEnabled);
          reconcile(settings.computer_control_enabled ?? false, fields.computerControlEnabled, fields.savedComputerControlEnabled, setComputerControlEnabled, setSavedComputerControlEnabled);
          reconcile(settings.exa_api_key ?? "", fields.exaApiKey, fields.savedExaApiKey, setExaApiKey, setSavedExaApiKey);
          reconcile(settings.composio_api_key ?? "", fields.composioApiKey, fields.savedComposioApiKey, setComposioApiKey, setSavedComposioApiKey);
          reconcile(settings.jina_api_key ?? "", fields.jinaApiKey, fields.savedJinaApiKey, setJinaApiKey, setSavedJinaApiKey);
          reconcile(settings.firecrawl_api_key ?? "", fields.firecrawlApiKey, fields.savedFirecrawlApiKey, setFirecrawlApiKey, setSavedFirecrawlApiKey);
          reconcile(settings.web_fetch_proxy_url ?? "", fields.webFetchProxyUrl, fields.savedWebFetchProxyUrl, setWebFetchProxyUrl, setSavedWebFetchProxyUrl);
        })
        .catch(() => {});
    });
  }, [open]);

  // Initialise the settings agent when the dialog is open and none is chosen yet — this
  // also covers agents arriving after the dialog opened. Adjusting state during render
  // (rather than in an effect) is the sanctioned pattern here; the `!settingsAgent` guard
  // makes it a one-shot that converges, so it never loops.
  if (open && !settingsAgent) {
    const nextAgent = selectedAgent || agents[0]?.id || "";
    if (nextAgent) setSettingsAgent(nextAgent);
  }

  // Live sync of the agent section when an agent config file changes on disk. Skipped
  // while the user has unsaved agent edits, so their in-progress changes are preserved.
  const agentSyncRef = useRef({ settingsAgent, workingDirectory, agentDirty });
  useEffect(() => {
    agentSyncRef.current = { settingsAgent, workingDirectory, agentDirty };
  });
  useEffect(() => {
    if (!open) return;
    return subscribeEvents((event) => {
      if (event.type !== "agents_changed") return;
      const { settingsAgent: agent, workingDirectory: directory, agentDirty: dirty } = agentSyncRef.current;
      if (!agent || dirty) return;
      fetchAgentConfiguration(agent, directory)
        .then((configuration) => {
          setAgentConfiguration(configuration);
          setSavedAgentConfiguration(configuration);
        })
        .catch(() => {});
    });
  }, [open]);

  // The agent+folder the config should reflect while the dialog is open. `agentLoading`
  // is derived: the request is in flight until `loadedAgentKey` catches up to this.
  const agentRequestKey = open && settingsAgent ? `${settingsAgent}\u0000${workingDirectory}` : "";
  const agentLoading = agentRequestKey !== "" && loadedAgentKey !== agentRequestKey;

  // Load the selected agent's configuration whenever the open dialog's agent or folder
  // changes. The effect only sets state when the fetch resolves, so there is no
  // synchronous loading-flag cascade — the derived `agentLoading` covers the in-flight
  // window (masking any stale error until the new result lands).
  useEffect(() => {
    if (!agentRequestKey) return;
    let cancelled = false;
    fetchAgentConfiguration(settingsAgent, workingDirectory)
      .then((configuration) => {
        if (cancelled) return;
        setAgentConfiguration(configuration);
        setSavedAgentConfiguration(configuration);
        setAgentError("");
      })
      .catch((error) => {
        if (cancelled) return;
        setAgentConfiguration(null);
        setSavedAgentConfiguration(null);
        setAgentError(error instanceof Error ? error.message : translation("loadAgentError"));
      })
      .finally(() => {
        if (!cancelled) setLoadedAgentKey(agentRequestKey);
      });
    return () => {
      cancelled = true;
    };
  }, [agentRequestKey, settingsAgent, workingDirectory, translation]);

  async function handleSave() {
    setSaving(true);
    try {
      const nextExaApiKey = exaApiKey.trim();
      const nextComposioApiKey = composioApiKey.trim();
      const nextJinaApiKey = jinaApiKey.trim();
      const nextFirecrawlApiKey = firecrawlApiKey.trim();
      const nextWebFetchProxyUrl = webFetchProxyUrl.trim();
      if (generalDirty) {
        await saveSettings({
          permission_mode: permissionMode,
          sandbox: { enforce: sandboxEnforce },
          exa_api_key: nextExaApiKey,
          composio_api_key: nextComposioApiKey,
          jina_api_key: nextJinaApiKey,
          firecrawl_api_key: nextFirecrawlApiKey,
          web_fetch_proxy_url: nextWebFetchProxyUrl,
          provider_keys: {},
          provider_base_urls: {},
          workspace_strategy: workspaceStrategy,
        });
        setExaApiKey(nextExaApiKey);
        setSavedExaApiKey(nextExaApiKey);
        setComposioApiKey(nextComposioApiKey);
        setSavedComposioApiKey(nextComposioApiKey);
        setJinaApiKey(nextJinaApiKey);
        setSavedJinaApiKey(nextJinaApiKey);
        setFirecrawlApiKey(nextFirecrawlApiKey);
        setSavedFirecrawlApiKey(nextFirecrawlApiKey);
        setWebFetchProxyUrl(nextWebFetchProxyUrl);
        setSavedWebFetchProxyUrl(nextWebFetchProxyUrl);
        setSavedPermissionMode(permissionMode);
        setSavedSandboxEnforce(sandboxEnforce);
        setSavedWorkspaceStrategy(workspaceStrategy);
        onPermissionModeChange?.(permissionMode);
        void onSandboxEnforceChange?.(sandboxEnforce);
        void onWorkspaceStrategyChange?.(workspaceStrategy);
      }
      if (autoCompaction !== savedAutoCompaction) {
        await updateCompactionSettings({ auto: autoCompaction });
        setSavedAutoCompaction(autoCompaction);
      }
      if (userContextEnabled !== savedUserContextEnabled) {
        await updateUserContextSetting(userContextEnabled);
        setSavedUserContextEnabled(userContextEnabled);
      }
      if (computerControlEnabled !== savedComputerControlEnabled) {
        await updateComputerControlSetting(computerControlEnabled);
        setSavedComputerControlEnabled(computerControlEnabled);
      }
      if (agentDirty && settingsAgent && agentConfiguration) {
        const savedConfiguration = await saveAgentConfiguration(settingsAgent, {
          permission_mode: agentConfiguration.permission_mode,
          model: agentConfiguration.model,
          provider: agentConfiguration.provider,
          reasoning_effort: agentConfiguration.reasoning_effort,
          tools_enabled: agentConfiguration.tools_enabled,
          bash: agentConfiguration.bash,
        }, workingDirectory);
        setAgentConfiguration(savedConfiguration);
        setSavedAgentConfiguration(savedConfiguration);
        onAgentChange?.(settingsAgent);
      }
      if (connectionDirty) {
        setDiscardConfirmOpen(true);
      } else {
        onOpenChange(false);
      }
    } finally {
      setSaving(false);
    }
  }

  function requestClose() {
    if (hasUnsavedChanges) {
      setDiscardConfirmOpen(true);
      return;
    }
    onOpenChange(false);
  }

  function discardChangesAndClose() {
    setExaApiKey(savedExaApiKey);
    setComposioApiKey(savedComposioApiKey);
    setJinaApiKey(savedJinaApiKey);
    setFirecrawlApiKey(savedFirecrawlApiKey);
    setWebFetchProxyUrl(savedWebFetchProxyUrl);
    setPermissionMode(savedPermissionMode);
    setSandboxEnforce(savedSandboxEnforce);
    setWorkspaceStrategy(savedWorkspaceStrategy);
    setAutoCompaction(savedAutoCompaction);
    setUserContextEnabled(savedUserContextEnabled);
    setComputerControlEnabled(savedComputerControlEnabled);
    setAgentConfiguration(savedAgentConfiguration);
    setSettingsAgent(selectedAgent || agents[0]?.id || "");
    setConnectionResetToken((current) => current + 1);
    setConnectionDirty(false);
    setDiscardConfirmOpen(false);
    onOpenChange(false);
  }

  const { theme: appTheme, setTheme: setAppTheme } = useColorMode();
  const { locale, setLocale } = useLocale();
  const [settingsSearch, setSettingsSearch] = useState("");
  const query = settingsSearch.trim().toLowerCase();
  const searching = query.length > 0;
  // Soft top/bottom fades on the content pane, matching the sessions sidebar's scroll edges.
  const { containerRef: contentScrollRef, onScroll: onContentScroll, fade: contentFade } = useScrollEdgeFade();

  // Every setting modeled as data so the search box can match titles/descriptions across
  // sections. A "page" is one left-nav entry; it holds titled sections of rows (title +
  // description on the left, control on the right) plus optional full-width blocks for the
  // richer editors that aren't a single control (locations, agent permissions, connection).
  const grantAlerts = ((userContextEnabled && fullDiskAccess === false) || accessibilityGranted === false) ? (
    <Flex direction="column" gap={1.5}>
      {userContextEnabled && fullDiskAccess === false && (
        <Alert.Root status="warning" size="sm" borderRadius="md" alignItems="center">
          <Alert.Indicator />
          <Alert.Content flex={1} minW={0}>
            <Alert.Description fontSize="xs">{translation("fullDiskAccessBody")}</Alert.Description>
          </Alert.Content>
          <Button colorPalette="orange" variant="solid" flexShrink={0} onClick={() => void openFullDiskAccessSettings()}>
            {translation("grantFullDiskAccess")}
          </Button>
        </Alert.Root>
      )}
      {accessibilityGranted === false && (
        <Alert.Root status="warning" size="sm" borderRadius="md" alignItems="center">
          <Alert.Indicator />
          <Alert.Content flex={1} minW={0}>
            <Alert.Description fontSize="xs">{translation("computerControlBody")}</Alert.Description>
          </Alert.Content>
          <Button colorPalette="orange" variant="solid" flexShrink={0} onClick={() => {
            try { localStorage.setItem("daisy:pendingComputerControlEnable", "1"); } catch { /* private mode */ }
            setAwaitingGrantReturn(true);
            void openAccessibilitySettings();
          }}>
            {translation("grantAccessibility")}
          </Button>
        </Alert.Root>
      )}
    </Flex>
  ) : null;

  const secretRow = (label: string, placeholder: string, value: string, onChange: (next: string) => void, description?: string): SettingRowDef => ({
    key: label, title: label, description,
    control: (
      <Box w={{ base: "100%", sm: "260px" }}>
        <SecretField label={label} placeholder={placeholder} value={value} disabled={saving} onChange={onChange} hideLabel />
      </Box>
    ),
    layout: "stacked",
  });

  const pages: SettingsPage[] = [
    {
      id: "general", label: translation("tabGeneral"), icon: <LuKeyRound size={14} />,
      sections: [
        {
          title: translation("runtime"),
          rows: [
            { key: "approvalMode", title: translation("approvalMode"), control: <PermissionModeControl value={permissionMode} onChange={setPermissionMode} /> },
            { key: "filesystemProtection", title: translation("filesystemProtection"), description: sandboxBackend.backend ? undefined : translation("filesystemProtectionUnavailable"), control: <SandboxToggleControl enforce={sandboxEnforce} backend={sandboxBackend.backend} onChange={setSandboxEnforce} /> },
            { key: "gitWorkspace", title: translation("gitWorkspace"), control: <WorkspaceStrategyControl value={workspaceStrategy} onChange={setWorkspaceStrategy} /> },
            { key: "compaction", title: translation("compaction"), control: <CompactionToggleControl enabled={autoCompaction} onChange={setAutoCompaction} /> },
            { key: "userContext", title: translation("userContext"), description: translation("userContextHint"), control: <UserContextToggleControl enabled={userContextEnabled} onChange={setUserContextEnabled} /> },
            { key: "computerControl", title: translation("computerControl"), description: translation("computerControlHint"), control: <ComputerControlToggleControl enabled={computerControlEnabled} onChange={accessibilityGranted ? setComputerControlEnabled : undefined} /> },
          ],
          block: grantAlerts,
        },
        {
          title: translation("appearance"),
          rows: [
            { key: "theme", title: translation("theme.label"), control: (
              <Box w="200px"><SimpleSelect items={[{ value: "system", label: translation("theme.system") }, { value: "light", label: translation("theme.light") }, { value: "dark", label: translation("theme.dark") }]} value={appTheme} onValueChange={(next) => setAppTheme(next as "system" | "light" | "dark")} /></Box>
            ) },
            { key: "language", title: translation("language.label"), control: (
              <Box w="200px"><SimpleSelect items={LOCALES.map((code) => ({ value: code, label: translation(`language.${code}`) }))} value={locale} onValueChange={(next) => setLocale(next as Locale)} /></Box>
            ) },
          ],
        },
        {
          title: translation("apiKeys"),
          rows: [
            secretRow(translation("exaApiKey"), "xxxxxxxx-...", exaApiKey, setExaApiKey),
            secretRow(translation("composioApiKey"), "cmp_...", composioApiKey, setComposioApiKey),
            secretRow(translation("jinaApiKey"), "jina_...", jinaApiKey, setJinaApiKey, translation("jinaApiKeyHint")),
            secretRow(translation("firecrawlApiKey"), "fc-...", firecrawlApiKey, setFirecrawlApiKey, translation("firecrawlApiKeyHint")),
            secretRow(translation("proxyServer"), "http://user:pass@host:port", webFetchProxyUrl, setWebFetchProxyUrl, translation("proxyServerHint")),
          ],
        },
        // The two subscription sign-ins, stacked: neither takes an API key, so neither
        // belongs in the rows above, and they are independent of each other.
        { title: translation("modelProviders"), rows: [], block: (
          <Flex direction="column" gap={5} maxW="520px"><ChatGPTAuthControl /><CursorAuthControl /></Flex>
        ) },
      ],
    },
    ...(projectId ? [{
      id: "locations" as SettingsSection, label: translation("tabLocations"), icon: <LuServer size={14} />,
      sections: [{ title: translation("locations"), rows: [], block: <ProjectLocationsPanel projectId={projectId} /> }],
    }] : []),
    {
      id: "agents", label: translation("tabAgents"), icon: <LuUsers size={14} />,
      sections: [
        {
          title: translation("agent"),
          rows: [{ key: "profile", title: translation("profile"), control: <Box w="280px"><SimpleSelect items={agentItems} value={settingsAgent} onValueChange={setSettingsAgent} placeholder={translation("chooseAgent")} /></Box> }],
          block: agentLoading ? (
            <Flex align="center" gap={2} color="fg.muted" fontSize="sm" py={3}><Spinner size="xs" />{translation("loadingAgentConfiguration")}</Flex>
          ) : agentError ? (
            <Text fontSize="sm" color="red.fg">{agentError}</Text>
          ) : agentConfiguration ? (
            <AgentPermissionsEditor configuration={agentConfiguration} models={models} providers={modelProviders} recentModels={recentModels} onChange={setAgentConfiguration} />
          ) : null,
        },
        { title: "External agents", rows: [], block: <RemoteAgentsPanel /> },
      ],
    },
    {
      id: "connection", label: translation("tabConnection"), icon: <LuPlug size={14} />,
      sections: [{
        rows: [],
        block: (
          <Flex direction="column" gap={4}>
            <Box>
              <Text textStyle="fieldLabel" mb={1}>{translation("currentConnection")}</Text>
              <ConnectionSwitcher currentTargetId={currentConnectionId} onConnectionChange={onConnectionChange} onOpenConnectionSettings={() => undefined} />
            </Box>
            <ConnectionSettings key={connectionResetToken} variant="dialog" currentTargetId={currentConnectionId} onDirtyChange={setConnectionDirty} onConnected={(target) => { onConnectionChange?.(target); onOpenChange(false); }} />
          </Flex>
        ),
      }],
    },
  ];

  const activePage = pages.find((page) => page.id === section) ?? pages[0];
  const rowMatches = (row: SettingRowDef) => `${row.title} ${row.description ?? ""}`.toLowerCase().includes(query);
  // When searching, collapse every page's sections into just those with matching rows, so
  // results read as a flat filtered list regardless of which nav entry they live under.
  const searchSections = searching
    ? pages.flatMap((page) => page.sections
        .map((section) => ({ ...section, rows: section.rows.filter(rowMatches), block: undefined }))
        .filter((section) => section.rows.length > 0))
    : [];

  return (
    <>
    <Dialog.Root open={open} onOpenChange={(event) => event.open ? onOpenChange(true) : requestClose()} placement="center">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content
            data-layout="settings-dialog"
            w={{ base: "calc(100vw - 16px)", md: "min(900px, calc(100vw - 48px))" }}
            maxW="900px"
            h={{ base: "calc(100dvh - 16px)", md: "min(760px, calc(100vh - 48px))" }}
            display="flex"
            flexDirection={{ base: "column", md: "row" }}
            overflow="hidden"
            p={0}
          >
            {/* Left nav spans the dialog's full height (flush to the rounded top/bottom edges),
                beside a column that carries the header, the scrolling content, and the footer —
                so its tint never gets cut off between a separate header and footer. */}
                <Flex
                  data-layout="settings-navigation"
                  direction="column"
                  w={{ base: "full", md: 52 }}
                  flexShrink={0}
                  borderRightWidth={{ base: 0, md: "1px" }}
                  borderBottomWidth={{ base: "1px", md: 0 }}
                  borderColor="border.muted"
                  minH={0}
                  bg="bg.subtle"
                >
                  <Box p={3} flexShrink={0}>
                    <Flex align="center" gap={2} h={8} px={2} borderRadius="md" bg="bg" borderWidth="1px" borderColor="border.muted" _focusWithin={{ borderColor: "border.emphasized" }}>
                      <Box color="fg.muted" flexShrink={0} display="flex" alignItems="center"><LuSearch size={14} /></Box>
                      <Input
                        border="none"
                        size="xs"
                        h="full"
                        px={0}
                        placeholder={translation("searchPlaceholder")}
                        aria-label={translation("searchPlaceholder")}
                        value={settingsSearch}
                        onChange={(event) => setSettingsSearch(event.target.value)}
                        _focusVisible={{ boxShadow: "none", outline: "none" }}
                      />
                    </Flex>
                  </Box>
                  <Box flex={1} minH={0} overflowY="auto" px={3} pb={3}>
                    <Text pb={2} textStyle="sectionLabel">{translation("title")}</Text>
                    <Flex direction="column" gap={1}>
                      {pages.map((page) => {
                        const active = !searching && page.id === activePage.id;
                        return (
                          <Button
                            variant="ghost"
                            key={page.id}
                            w={{ base: "auto", md: "full" }}
                            flexShrink={0}
                            gap={1.5}
                            minH="30px"
                            px={2}
                            justifyContent="flex-start"
                            textAlign="left"
                            color={active ? "fg" : "fg.muted"}
                            fontWeight={active ? "medium" : "normal"}
                            bg={active ? "bg.muted" : undefined}
                            _hover={{ bg: active ? "bg.muted" : "bg.emphasized", color: "fg" }}
                            transition="background-color 0.12s, color 0.12s"
                            onClick={() => { setSettingsSearch(""); onSectionChange(page.id); }}
                          >
                            <Box color={active ? "fg" : "fg.subtle"} flexShrink={0} display="flex" alignItems="center">{page.icon}</Box>
                            <Text flex={1} minW={0} truncate fontSize="xs">{page.label}</Text>
                          </Button>
                        );
                      })}
                    </Flex>
                  </Box>
                </Flex>
            <Flex data-layout="settings-content" direction="column" flex={1} minW={0} minH={0}>
              <Dialog.Body px={0} py={2} flex={1} minH={0}>
                {/* Right content: search results across all sections, or the active page's sections. */}
                <Box ref={contentScrollRef} onScroll={onContentScroll} css={contentFade} h="100%" overflowY="auto" px={6} py={4}>
                  {searching ? (
                    searchSections.length === 0 ? (
                      <Flex h="full" align="center" justify="center" py={10}>
                        <EmptyState.Root size="sm">
                          <EmptyState.Content>
                            <EmptyState.Indicator>
                              <LuSearch />
                            </EmptyState.Indicator>
                            <VStack gap={0}>
                              <EmptyState.Title fontSize="sm">{translation("searchNoResultsTitle")}</EmptyState.Title>
                              <EmptyState.Description fontSize="xs">{translation("searchNoResults", { query: settingsSearch })}</EmptyState.Description>
                            </VStack>
                          </EmptyState.Content>
                        </EmptyState.Root>
                      </Flex>
                    ) : (
                      searchSections.map((section, index) => (
                        <SettingsSection key={index} title={section.title}>
                          {section.rows.map((row) => (
                            <SettingRow key={row.key} title={row.title} description={row.description} layout={row.layout}>{row.control}</SettingRow>
                          ))}
                        </SettingsSection>
                      ))
                    )
                  ) : (
                    pages.map((page) => (
                      <Box key={page.id} display={page.id === activePage.id ? "block" : "none"}>
                        {page.sections.map((pageSection, pageSectionIndex) => (
                          <SettingsSection key={pageSectionIndex} title={pageSection.title}>
                            {pageSection.rows.map((row) => (
                              <SettingRow key={row.key} title={row.title} description={row.description} layout={row.layout}>{row.control}</SettingRow>
                            ))}
                            {/* A block after rows is separated from the last row's divider by top
                                padding; a standalone block (no rows) sits flush under the heading. */}
                            {pageSection.block ? <Box pt={pageSection.rows.length > 0 ? 4 : 0}>{pageSection.block}</Box> : null}
                          </SettingsSection>
                        ))}
                      </Box>
                    ))
                  )}
                </Box>
              </Dialog.Body>
            {/* Footer sits inside the right column so the nav stays full height. */}
            <Dialog.Footer pt={3}>
              <Button variant="outline" onClick={requestClose} disabled={saving}>
                {tc("close")}
              </Button>
              {section !== "connection" && section !== "locations" && (
                <Button
                  colorPalette="blue"
                  onClick={handleSave}
                  loading={saving}
                  loadingText={tc("savingChanges")}
                  disabled={!hasUnsavedChanges}
                >
                  {tc("saveChanges")}
                </Button>
              )}
            </Dialog.Footer>
            </Flex>
            <Dialog.CloseTrigger />
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
    <ConfirmDialog
      open={discardConfirmOpen}
      onOpenChange={setDiscardConfirmOpen}
      title={translation("discardTitle")}
      cancelLabel={translation("keepEditing")}
      confirmLabel={translation("discard")}
      danger
      maxW="420px"
      onConfirm={discardChangesAndClose}
    >
      {translation("discardBody")}
    </ConfirmDialog>
    <ConfirmDialog
      open={restartPromptOpen}
      onOpenChange={setRestartPromptOpen}
      title={translation("restartTitle")}
      cancelLabel={translation("restartLater")}
      confirmLabel={translation("restartConfirm")}
      maxW="420px"
      onConfirm={() => void (async () => {
        // Two processes, two restarts. The daemon is the one macOS has to re-evaluate — the
        // trust check is cached per process and its workers are re-execs of it — and it is no
        // longer this app's child, so restarting the window alone would leave the grant unseen.
        // Restart it first, then reload the window against the successor.
        try {
          await restartDaemon();
        } catch {
          // A daemon that cannot be asked is one the user will have to restart themselves;
          // reloading the window is still the right second half, so carry on.
        }
        await restartApp();
      })()}
    >
      {translation("restartBody")}
    </ConfirmDialog>
    </>
  );
}

function SettingsGroup({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Flex direction="column" gap={3} minW={0}>
      <Text textStyle="panelTitle" color="fg">
        {title}
      </Text>
      <Flex direction="column" gap={3} minW={0}>
        {children}
      </Flex>
    </Flex>
  );
}

function SettingField({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <Box minW={0}>
      <Text textStyle="fieldLabel" color="fg" mb={hint ? 0.5 : 1}>
        {label}
      </Text>
      {hint ? (
        <Text fontSize="xs" color="fg.muted" mb={1.5}>
          {hint}
        </Text>
      ) : null}
      {children}
    </Box>
  );
}

function AgentPermissionsEditor({
  configuration,
  models,
  providers,
  recentModels,
  onChange,
}: {
  configuration: AgentConfiguration;
  models: ModelOption[];
  providers: ProviderOption[];
  recentModels: RecentModel[];
  onChange: (configuration: AgentConfiguration) => void;
}) {
  const translation = useTranslations("SettingsDialog");
  const ruleDecisionItems = useMemo(
    () => [
      { label: translation("ruleAllow"), value: "allow" },
      { label: translation("ruleAsk"), value: "ask" },
      { label: translation("ruleDeny"), value: "deny" },
    ],
    [translation],
  );
  const rules = Object.entries(configuration.bash.permissions).sort(([leftPattern], [rightPattern]) => leftPattern.localeCompare(rightPattern));

  function updateConfiguration(nextConfiguration: Partial<AgentConfiguration>) {
    onChange({ ...configuration, ...nextConfiguration });
  }

  function updateModel(modelIdentifier: string) {
    const [provider = "", ...modelParts] = modelIdentifier.split("/");
    updateConfiguration({
      provider,
      model: modelParts.join("/"),
    });
  }

  function updateBash(nextBash: Partial<AgentConfiguration["bash"]>) {
    updateConfiguration({ bash: { ...configuration.bash, ...nextBash } });
  }

  function updateRulePattern(previousPattern: string, nextPattern: string) {
    const nextPermissions = { ...configuration.bash.permissions };
    delete nextPermissions[previousPattern];
    if (nextPattern.trim()) nextPermissions[nextPattern.trim()] = configuration.bash.permissions[previousPattern] ?? "ask";
    updateBash({ permissions: nextPermissions });
  }

  function updateRuleDecision(pattern: string, decision: string) {
    updateBash({ permissions: { ...configuration.bash.permissions, [pattern]: decision } });
  }

  function deleteRule(pattern: string) {
    const nextPermissions = { ...configuration.bash.permissions };
    delete nextPermissions[pattern];
    updateBash({ permissions: nextPermissions });
  }

  function addRule() {
    let pattern = "command *";
    let suffix = 2;
    while (configuration.bash.permissions[pattern]) {
      pattern = `command ${suffix} *`;
      suffix += 1;
    }
    updateBash({ permissions: { ...configuration.bash.permissions, [pattern]: "ask" } });
  }

  return (
    <Flex direction="column" gap={4}>
      <SettingsGroup title={translation("defaults")}>
        <Flex gap={3} wrap="wrap" align="flex-start">
          <SettingField label={translation("configuredModel")}>
            <Box maxW="520px">
              <ModelSelect
                models={models}
                providers={providers}
                recent={recentModels}
                value={configuration.provider && configuration.model ? `${configuration.provider}/${configuration.model}` : ""}
                onChange={updateModel}
              />
            </Box>
          </SettingField>
          <SettingField label={translation("permissionMode")}>
            <PermissionModeControl
              value={configuration.permission_mode}
              onChange={(permissionModeValue) => updateConfiguration({ permission_mode: permissionModeValue })}
            />
          </SettingField>
        </Flex>
      </SettingsGroup>
      <SettingsGroup title={translation("tools")}>
        <Flex gap={3} wrap="wrap" align="flex-start">
          <SettingField label={translation("bash")}>
            <Flex gap={2} minW={0} wrap="wrap">
              <Button
                h={8}
                justifyContent="flex-start"
                variant={configuration.bash.enabled ? "subtle" : "outline"}
                colorPalette={configuration.bash.enabled ? "green" : "gray"}
                onClick={() => updateBash({ enabled: !configuration.bash.enabled })}
              >
                {configuration.bash.enabled ? translation("enabled") : translation("disabled")}
              </Button>
              <Button
                h={8}
                justifyContent="flex-start"
                variant={configuration.bash.background_allowed ? "subtle" : "outline"}
                colorPalette={configuration.bash.background_allowed ? "blue" : "gray"}
                onClick={() => updateBash({ background_allowed: !configuration.bash.background_allowed })}
              >
                {configuration.bash.background_allowed ? translation("backgroundAllowed") : translation("backgroundBlocked")}
              </Button>
            </Flex>
          </SettingField>
        </Flex>
      </SettingsGroup>
      <SettingsGroup title={translation("commandPermissions")}>
        <SettingField label={translation("rules")}>
          <Flex direction="column" gap={2} minW={0}>
            {rules.map(([pattern, decision], ruleIndex) => (
              <Flex key={ruleIndex} gap={2} align="center" minW={0}>
                <Input
                  fontFamily="var(--app-font-mono)"
                  fontSize="xs"
                  value={pattern}
                  onChange={(event) => updateRulePattern(pattern, event.target.value)}
                />
                <Box w="118px" flexShrink={0}>
                  <SimpleSelect
                    items={ruleDecisionItems}
                    value={decision}
                    onValueChange={(next) => updateRuleDecision(pattern, next)}
                  />
                </Box>
                <IconButton
                  aria-label={translation("deleteRule", { pattern })}
                  variant="ghost"
                  colorPalette="red"
                  flexShrink={0}
                  onClick={() => deleteRule(pattern)}
                >
                  <LuTrash2 size={14} />
                </IconButton>
              </Flex>
            ))}
            <Button variant="outline" justifyContent="flex-start" onClick={addRule}>
              <LuPlus size={14} />
              {translation("addRule")}
            </Button>
          </Flex>
        </SettingField>
      </SettingsGroup>
    </Flex>
  );
}

function SecretField({
  label,
  placeholder,
  value,
  disabled,
  onChange,
  hideLabel = false,
}: {
  label: string;
  placeholder: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  hideLabel?: boolean;
}) {
  const translation = useTranslations("SettingsDialog");
  const [visible, setVisible] = useState(false);
  return (
    <Box>
      {!hideLabel && (
        <Text textStyle="fieldLabel" mb={1}>
          {label}
        </Text>
      )}
      <Flex gap={1.5} align="center">
        <Input
          type={visible ? "text" : "password"}
          fontFamily="var(--app-font-mono)"
          fontSize="xs"
          placeholder={placeholder}
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
        <IconButton
          aria-label={visible ? translation("hide") : translation("show")}
          variant="ghost"
          flexShrink={0}
          onClick={() => setVisible((current) => !current)}
        >
          {visible ? <LuEyeOff size={14} /> : <LuEye size={14} />}
        </IconButton>
      </Flex>
    </Box>
  );
}
