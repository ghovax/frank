"use client";

import {
  Alert,
  Box,
  Button,
  Dialog,
  EmptyState,
  Flex,
  IconButton,
  Input,
  Portal,
  Spinner,
  Text,
  VStack,
} from "@chakra-ui/react";
import { swallowed } from "@/lib/swallowed";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  LuClock,
  LuEye,
  LuEyeOff,
  LuKeyRound,
  LuMonitor,
  LuPlus,
  LuSearch,
  LuServer,
  LuTrash2,
  LuUsers,
} from "react-icons/lu";
import {
  fetchAccessibility,
  fetchAgentConfiguration,
  fetchFullDiskAccess,
  fetchSettings,
  openAccessibilitySettings,
  openFullDiskAccessSettings,
  restartApp,
  restartDaemon,
  saveAgentConfiguration,
  saveSettings,
  subscribeEvents,
  updateCompactionSettings,
  updateComputerControlSetting,
  updateDictationSetting,
  updateToolboxSetting,
  updateUserContextSetting,
  type AgentConfiguration,
  type AgentSummary,
  type ModelOption,
  type PermissionMode,
  type ProviderOption,
  type RecentModel,
  type SandboxEnforce,
} from "@/lib/api";
import { ModelSelect } from "./model-select";
import { ChatGPTAuthControl } from "./chatgpt-auth";
import { CursorAuthControl } from "./cursor-auth";
import { WorkspaceLocationsPanel } from "./workspace-locations";
import { RemoteAgentsPanel } from "./remote-agents-panel";
import { MachinesPanel } from "./machines-panel";
import { SchedulesPanel } from "./schedules-panel";
import { SimpleSelect } from "./ui/simple-select";
import { useColorMode } from "./ui/color-mode";
import { ConfirmDialog } from "./ui/confirm-dialog";
import { useTranslations } from "next-intl";
import { useLocale } from "@/lib/i18n/locale-provider";
import { LOCALES, type Locale } from "@shared/locales";
import { useSchemaSettingsPage } from "./settings-schema-pages";
import {
  AgentSelectControl,
  CompactionToggleControl,
  ComputerControlToggleControl,
  DictationToggleControl,
  PermissionModeControl,
  SandboxToggleControl,
  ToolboxToggleControl,
  UserContextToggleControl,
  WorktreeStrategyControl,
  type WorktreeStrategyValue,
} from "./session-controls";
import { useScrollEdgeFade } from "@/lib/scroll-fade";
import { Section } from "./ui/semantic";
import { errorMessage } from "@/lib/errors";
import { usePreferences } from "@/lib/preferences";

// The panel's own pages, plus one per section of the configuration file, which is not knowable here.
export type SettingsSection = string;

import type { SettingRowDef, SettingsPage } from "@/lib/settings-model";

// A titled section: a heading over a stack of rows separated by hairline dividers.
function SettingsSection({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <Section mb={8} _last={{ mb: 0 }}>
      {title ? (
        <Text fontSize="sm" fontWeight="semibold" color="fg" mb={2}>
          {title}
        </Text>
      ) : null}
      <Box
        css={{
          "& > *": { borderColor: "var(--chakra-colors-border-muted)" },
          "& > *:not(:last-child)": { borderBottomWidth: "1px" },
        }}
      >
        {children}
      </Box>
    </Section>
  );
}

// One settings row: label and description on the left, control on the right, or stacked for wide inputs.
function SettingRow({
  title,
  description,
  children,
  layout = "row",
}: {
  title: string;
  description?: string;
  children: ReactNode;
  layout?: "row" | "stacked";
}) {
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
        <Text textStyle="fieldLabel" color="fg">
          {title}
        </Text>
        {description ? (
          <Text fontSize="xs" color="fg.muted" mt={0.5}>
            {description}
          </Text>
        ) : null}
      </Box>
      <Box
        flexShrink={{ base: 1, sm: 0 }}
        alignSelf={{ base: "stretch", sm: stacked ? "stretch" : undefined }}
      >
        {children}
      </Box>
    </Flex>
  );
}

// A dialog for entering credentials and configuring behaviour, applied live on save.
export function SettingsDialog({
  open,
  onOpenChange,
  section,
  onSectionChange,
  workspaceId = "",
  workingDirectory = "",
  models = [],
  modelProviders = [],
  recentModels = [],
  agents = [],
  selectedAgent = "",
  onAgentChange,
  livePermissionMode = "ask",
  liveSandboxEnforce = "required" as SandboxEnforce,
  sandboxBackend = { backend: "", detail: "" },
  liveWorktreeStrategy = "none",
  onPermissionModeChange,
  onSandboxEnforceChange,
  onWorktreeStrategyChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  workspaceId?: string;
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
  liveWorktreeStrategy?: WorktreeStrategyValue;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  onSandboxEnforceChange?: (enforce: SandboxEnforce) => void | Promise<void>;
  onWorktreeStrategyChange?: (strategy: WorktreeStrategyValue) => void | Promise<void>;
}) {
  const translation = useTranslations("SettingsDialog");
  const tc = useTranslations("Common");
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(livePermissionMode);
  const [savedPermissionMode, setSavedPermissionMode] =
    useState<PermissionMode>(livePermissionMode);
  const [sandboxEnforce, setSandboxEnforce] = useState<SandboxEnforce>(liveSandboxEnforce);
  const [savedSandboxEnforce, setSavedSandboxEnforce] =
    useState<SandboxEnforce>(liveSandboxEnforce);
  const [worktreeStrategy, setWorktreeStrategy] =
    useState<WorktreeStrategyValue>(liveWorktreeStrategy);
  const [savedWorktreeStrategy, setSavedWorktreeStrategy] =
    useState<WorktreeStrategyValue>(liveWorktreeStrategy);
  const [autoCompaction, setAutoCompaction] = useState(false);
  const [savedAutoCompaction, setSavedAutoCompaction] = useState(false);
  const [userContextEnabled, setUserContextEnabled] = useState(false);
  const [savedUserContextEnabled, setSavedUserContextEnabled] = useState(false);
  const [computerControlEnabled, setComputerControlEnabled] = useState(false);
  const [toolboxEnabled, setToolboxEnabled] = useState(false);
  const [savedToolboxEnabled, setSavedToolboxEnabled] = useState(false);
  // Whether the machine can offer a toolbox at all, which is a fact rather than a setting.
  const [toolboxAvailable, setToolboxAvailable] = useState(false);
  const [savedComputerControlEnabled, setSavedComputerControlEnabled] = useState(false);
  const [dictationEnabled, setDictationEnabled] = useState(false);
  const [savedDictationEnabled, setSavedDictationEnabled] = useState(false);
  // Whether the server can read Full-Disk-Access-protected data, `null` while unknown.
  const [fullDiskAccess, setFullDiskAccess] = useState<boolean | null>(null);
  const [accessibilityGranted, setAccessibilityGranted] = useState<boolean | null>(null);
  // After Grant access opens System Settings, returning focus offers a restart, since macOS only tells a fresh launch.
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
  const [settingsAgent, setSettingsAgent] = useState(selectedAgent);
  const [agentConfiguration, setAgentConfiguration] = useState<AgentConfiguration | null>(null);
  const [savedAgentConfiguration, setSavedAgentConfiguration] = useState<AgentConfiguration | null>(
    null,
  );
  const [agentError, setAgentError] = useState("");
  // The agent and folder the configuration was last resolved for, from which loading is derived rather than set.
  const [loadedAgentKey, setLoadedAgentKey] = useState("");
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const agentDirty = JSON.stringify(agentConfiguration) !== JSON.stringify(savedAgentConfiguration);
  const generalDirty =
    exaApiKey !== savedExaApiKey ||
    composioApiKey !== savedComposioApiKey ||
    jinaApiKey !== savedJinaApiKey ||
    firecrawlApiKey !== savedFirecrawlApiKey ||
    webFetchProxyUrl !== savedWebFetchProxyUrl ||
    permissionMode !== savedPermissionMode ||
    sandboxEnforce !== savedSandboxEnforce ||
    worktreeStrategy !== savedWorktreeStrategy ||
    autoCompaction !== savedAutoCompaction ||
    userContextEnabled !== savedUserContextEnabled ||
    computerControlEnabled !== savedComputerControlEnabled ||
    toolboxEnabled !== savedToolboxEnabled ||
    dictationEnabled !== savedDictationEnabled;
  const hasUnsavedChanges = generalDirty || agentDirty;

  // Pre-fill from the server each time the dialog opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchSettings()
      .then((settings) => {
        if (cancelled) return;
        setPermissionMode(settings.permission_mode);
        setSavedPermissionMode(settings.permission_mode);
        setSandboxEnforce(settings.sandbox.enforce);
        setSavedSandboxEnforce(settings.sandbox.enforce);
        setWorktreeStrategy(settings.worktree_strategy);
        setSavedWorktreeStrategy(settings.worktree_strategy);
        setAutoCompaction(settings.compaction?.automatic ?? false);
        setSavedAutoCompaction(settings.compaction?.automatic ?? false);
        setUserContextEnabled(settings.user_context_enabled);
        setSavedUserContextEnabled(settings.user_context_enabled);
        setComputerControlEnabled(settings.computer_control_enabled);
        setSavedComputerControlEnabled(settings.computer_control_enabled);
        setToolboxEnabled(settings.toolbox_enabled);
        setSavedToolboxEnabled(settings.toolbox_enabled);
        setToolboxAvailable(settings.toolbox_available);
        // Both halves of the dictation state are read from this payload, so the control reflects how the daemon is configured.
        setDictationEnabled(settings.dictation_enabled);
        setSavedDictationEnabled(settings.dictation_enabled);
        setExaApiKey(settings.exa_api_key);
        setSavedExaApiKey(settings.exa_api_key);
        setComposioApiKey(settings.composio_api_key);
        setSavedComposioApiKey(settings.composio_api_key);
        setJinaApiKey(settings.jina_api_key);
        setSavedJinaApiKey(settings.jina_api_key);
        setFirecrawlApiKey(settings.firecrawl_api_key);
        setSavedFirecrawlApiKey(settings.firecrawl_api_key);
        setWebFetchProxyUrl(settings.web_fetch_proxy_url);
        setSavedWebFetchProxyUrl(settings.web_fetch_proxy_url);
      })
      .catch((caught) =>
        swallowed({ component: "settings", operation: "read the settings" }, caught),
      );
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Check Full Disk Access only when it is relevant, and again whenever the window regains focus.
  useEffect(() => {
    // Only while the dialog is open with user-context on, since the banner is gated on that too.
    if (!open || !userContextEnabled) return;
    let cancelled = false;
    const check = () => {
      void fetchFullDiskAccess().then((granted) => {
        if (!cancelled) setFullDiskAccess(granted);
      });
    };
    check();
    window.addEventListener("focus", check);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", check);
    };
  }, [open, userContextEnabled]);

  // Accessibility must be granted before computer use can be enabled, so it is checked whenever the dialog is open.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const check = () => {
      void fetchAccessibility().then((granted) => {
        if (!cancelled) setAccessibilityGranted(granted);
      });
    };
    check();
    window.addEventListener("focus", check);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", check);
    };
  }, [open]);

  // Re-check accessibility whenever focus returns, and only offer the restart once it is actually granted.
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

  // Latest field values, mirrored into a ref so the live-sync subscription can read them without re-subscribing.
  const fieldsRef = useRef({
    permissionMode,
    savedPermissionMode,
    sandboxEnforce,
    savedSandboxEnforce,
    worktreeStrategy,
    savedWorktreeStrategy,
    autoCompaction,
    savedAutoCompaction,
    userContextEnabled,
    savedUserContextEnabled,
    computerControlEnabled,
    savedComputerControlEnabled,
    toolboxEnabled,
    savedToolboxEnabled,
    dictationEnabled,
    savedDictationEnabled,
    exaApiKey,
    savedExaApiKey,
    composioApiKey,
    savedComposioApiKey,
    jinaApiKey,
    savedJinaApiKey,
    firecrawlApiKey,
    savedFirecrawlApiKey,
    webFetchProxyUrl,
    savedWebFetchProxyUrl,
  });
  useEffect(() => {
    fieldsRef.current = {
      permissionMode,
      savedPermissionMode,
      sandboxEnforce,
      savedSandboxEnforce,
      worktreeStrategy,
      savedWorktreeStrategy,
      autoCompaction,
      savedAutoCompaction,
      userContextEnabled,
      savedUserContextEnabled,
      computerControlEnabled,
      savedComputerControlEnabled,
      toolboxEnabled,
      savedToolboxEnabled,
      dictationEnabled,
      savedDictationEnabled,
      exaApiKey,
      savedExaApiKey,
      composioApiKey,
      savedComposioApiKey,
      jinaApiKey,
      savedJinaApiKey,
      firecrawlApiKey,
      savedFirecrawlApiKey,
      webFetchProxyUrl,
      savedWebFetchProxyUrl,
    };
  });

  // Live sync while the dialog is open, moving each saved baseline to the new truth without clobbering unsaved edits.
  useEffect(() => {
    if (!open) return;
    return subscribeEvents((event) => {
      if (event.type !== "settings_changed") return;
      fetchSettings()
        .then((settings) => {
          const fields = fieldsRef.current;
          const reconcile = <T,>(
            next: T,
            current: T,
            saved: T,
            setCurrent: (value: T) => void,
            setSaved: (value: T) => void,
          ) => {
            setSaved(next);
            if (current === saved) setCurrent(next);
          };
          reconcile(
            settings.permission_mode,
            fields.permissionMode,
            fields.savedPermissionMode,
            setPermissionMode,
            setSavedPermissionMode,
          );
          reconcile(
            settings.sandbox.enforce,
            fields.sandboxEnforce,
            fields.savedSandboxEnforce,
            setSandboxEnforce,
            setSavedSandboxEnforce,
          );
          reconcile(
            settings.worktree_strategy,
            fields.worktreeStrategy,
            fields.savedWorktreeStrategy,
            setWorktreeStrategy,
            setSavedWorktreeStrategy,
          );
          reconcile(
            settings.compaction?.automatic ?? false,
            fields.autoCompaction,
            fields.savedAutoCompaction,
            setAutoCompaction,
            setSavedAutoCompaction,
          );
          reconcile(
            settings.user_context_enabled,
            fields.userContextEnabled,
            fields.savedUserContextEnabled,
            setUserContextEnabled,
            setSavedUserContextEnabled,
          );
          reconcile(
            settings.computer_control_enabled,
            fields.computerControlEnabled,
            fields.savedComputerControlEnabled,
            setComputerControlEnabled,
            setSavedComputerControlEnabled,
          );
          reconcile(
            settings.toolbox_enabled,
            fields.toolboxEnabled,
            fields.savedToolboxEnabled,
            setToolboxEnabled,
            setSavedToolboxEnabled,
          );
          setToolboxAvailable(settings.toolbox_available);
          reconcile(
            settings.dictation_enabled,
            fields.dictationEnabled,
            fields.savedDictationEnabled,
            setDictationEnabled,
            setSavedDictationEnabled,
          );
          reconcile(
            settings.exa_api_key,
            fields.exaApiKey,
            fields.savedExaApiKey,
            setExaApiKey,
            setSavedExaApiKey,
          );
          reconcile(
            settings.composio_api_key,
            fields.composioApiKey,
            fields.savedComposioApiKey,
            setComposioApiKey,
            setSavedComposioApiKey,
          );
          reconcile(
            settings.jina_api_key,
            fields.jinaApiKey,
            fields.savedJinaApiKey,
            setJinaApiKey,
            setSavedJinaApiKey,
          );
          reconcile(
            settings.firecrawl_api_key,
            fields.firecrawlApiKey,
            fields.savedFirecrawlApiKey,
            setFirecrawlApiKey,
            setSavedFirecrawlApiKey,
          );
          reconcile(
            settings.web_fetch_proxy_url,
            fields.webFetchProxyUrl,
            fields.savedWebFetchProxyUrl,
            setWebFetchProxyUrl,
            setSavedWebFetchProxyUrl,
          );
        })
        .catch((caught) =>
          swallowed({ component: "settings", operation: "read the agent configuration" }, caught),
        );
    });
  }, [open]);

  // Initialise the settings agent while the dialog is open, which also covers agents arriving after it opened.
  if (open && !settingsAgent) {
    const nextAgent = selectedAgent || agents[0]?.id || "";
    if (nextAgent) setSettingsAgent(nextAgent);
  }

  // Live sync of the agent section on disk changes, skipped while the user has unsaved agent edits.
  const agentSyncRef = useRef({ settingsAgent, workingDirectory, agentDirty });
  useEffect(() => {
    agentSyncRef.current = { settingsAgent, workingDirectory, agentDirty };
  });
  useEffect(() => {
    if (!open) return;
    return subscribeEvents((event) => {
      if (event.type !== "agents_changed") return;
      const {
        settingsAgent: agent,
        workingDirectory: directory,
        agentDirty: dirty,
      } = agentSyncRef.current;
      if (!agent || dirty) return;
      fetchAgentConfiguration(agent, directory)
        .then((configuration) => {
          setAgentConfiguration(configuration);
          setSavedAgentConfiguration(configuration);
        })
        .catch((caught) =>
          swallowed({ component: "settings", operation: "read the permission state" }, caught),
        );
    });
  }, [open]);

  // The agent and folder the configuration should reflect; loading is the gap until the loaded key catches up.
  const agentRequestKey = open && settingsAgent ? `${settingsAgent}\u0000${workingDirectory}` : "";
  const agentLoading = agentRequestKey !== "" && loadedAgentKey !== agentRequestKey;

  // Load the selected agent's configuration whenever the open dialog's agent or folder changes.
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
        setAgentError(errorMessage(error) || translation("loadAgentError"));
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
          worktree_strategy: worktreeStrategy,
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
        setSavedWorktreeStrategy(worktreeStrategy);
        onPermissionModeChange?.(permissionMode);
        void onSandboxEnforceChange?.(sandboxEnforce);
        void onWorktreeStrategyChange?.(worktreeStrategy);
      }
      if (autoCompaction !== savedAutoCompaction) {
        await updateCompactionSettings({ automatic: autoCompaction });
        setSavedAutoCompaction(autoCompaction);
      }
      if (userContextEnabled !== savedUserContextEnabled) {
        await updateUserContextSetting(userContextEnabled);
        setSavedUserContextEnabled(userContextEnabled);
      }
      if (toolboxEnabled !== savedToolboxEnabled) {
        await updateToolboxSetting(toolboxEnabled);
        setSavedToolboxEnabled(toolboxEnabled);
      }
      if (computerControlEnabled !== savedComputerControlEnabled) {
        await updateComputerControlSetting(computerControlEnabled);
        setSavedComputerControlEnabled(computerControlEnabled);
      }
      if (dictationEnabled !== savedDictationEnabled) {
        await updateDictationSetting(dictationEnabled);
        setSavedDictationEnabled(dictationEnabled);
      }
      if (agentDirty && settingsAgent && agentConfiguration) {
        const savedConfiguration = await saveAgentConfiguration(
          settingsAgent,
          {
            permission_mode: agentConfiguration.permission_mode,
            model: agentConfiguration.model,
            provider: agentConfiguration.provider,
            reasoning_effort: agentConfiguration.reasoning_effort,
            tools_enabled: agentConfiguration.tools_enabled,
            bash: agentConfiguration.bash,
          },
          workingDirectory,
        );
        setAgentConfiguration(savedConfiguration);
        setSavedAgentConfiguration(savedConfiguration);
        onAgentChange?.(settingsAgent);
      }
      onOpenChange(false);
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
    setWorktreeStrategy(savedWorktreeStrategy);
    setAutoCompaction(savedAutoCompaction);
    setUserContextEnabled(savedUserContextEnabled);
    setComputerControlEnabled(savedComputerControlEnabled);
    setAgentConfiguration(savedAgentConfiguration);
    setSettingsAgent(selectedAgent || agents[0]?.id || "");
    setDiscardConfirmOpen(false);
    onOpenChange(false);
  }

  const { theme: appTheme, setTheme: setAppTheme } = useColorMode();
  const { updatePreferences } = usePreferences();
  const { locale, setLocale } = useLocale();
  const [settingsSearch, setSettingsSearch] = useState("");
  const query = settingsSearch.trim().toLowerCase();
  const searching = query.length > 0;
  // Soft top/bottom fades on the content pane, matching the sessions sidebar's scroll edges.
  const {
    containerRef: contentScrollRef,
    onScroll: onContentScroll,
    fade: contentFade,
  } = useScrollEdgeFade();

  // Every setting modelled as data, so the search box can match titles and descriptions across sections.
  const grantAlerts =
    (userContextEnabled && fullDiskAccess === false) || accessibilityGranted === false ? (
      <Flex direction="column" gap={1.5}>
        {userContextEnabled && fullDiskAccess === false && (
          <Alert.Root status="warning" size="sm" borderRadius="md" alignItems="center">
            <Alert.Indicator />
            <Alert.Content flex={1} minW={0}>
              <Alert.Description fontSize="xs">
                {translation("fullDiskAccessBody")}
              </Alert.Description>
            </Alert.Content>
            <Button
              colorPalette="orange"
              variant="solid"
              flexShrink={0}
              onClick={() => void openFullDiskAccessSettings()}
            >
              {translation("grantFullDiskAccess")}
            </Button>
          </Alert.Root>
        )}
        {accessibilityGranted === false && (
          <Alert.Root status="warning" size="sm" borderRadius="md" alignItems="center">
            <Alert.Indicator />
            <Alert.Content flex={1} minW={0}>
              <Alert.Description fontSize="xs">
                {translation("computerControlBody")}
              </Alert.Description>
            </Alert.Content>
            <Button
              colorPalette="orange"
              variant="solid"
              flexShrink={0}
              onClick={() => {
                // Recorded on the daemon rather than here, since the grant only reaches a freshly started one.
                updatePreferences({ computer_control_awaiting_grant: true });
                setAwaitingGrantReturn(true);
                void openAccessibilitySettings();
              }}
            >
              {translation("grantAccessibility")}
            </Button>
          </Alert.Root>
        )}
      </Flex>
    ) : null;

  const secretRow = (
    label: string,
    placeholder: string,
    value: string,
    onChange: (next: string) => void,
    description?: string,
  ): SettingRowDef => ({
    key: label,
    title: label,
    description,
    control: (
      <Box w={{ base: "100%", sm: "260px" }}>
        <SecretField
          label={label}
          placeholder={placeholder}
          value={value}
          disabled={saving}
          onChange={onChange}
          hideLabel
        />
      </Box>
    ),
    layout: "stacked",
  });

  // The generated rows render through the panel's own row component, passed in rather than imported the other way.
  const renderSettingRow = useCallback(
    (row: SettingRowDef) => (
      <SettingRow key={row.key} title={row.title} description={row.description} layout={row.layout}>
        {row.control}
      </SettingRow>
    ),
    [],
  );
  const configurationPage = useSchemaSettingsPage(renderSettingRow);

  const curatedPages: SettingsPage[] = [
    {
      id: "general",
      label: translation("tabGeneral"),
      icon: <LuKeyRound size={14} />,
      sections: [
        {
          title: translation("runtime"),
          rows: [
            {
              key: "approvalMode",
              title: translation("approvalMode"),
              control: (
                <PermissionModeControl
                  value={permissionMode}
                  onChange={(next) => {
                    if (next) setPermissionMode(next);
                  }}
                />
              ),
            },
            {
              key: "filesystemProtection",
              title: translation("filesystemProtection"),
              description: sandboxBackend.backend
                ? undefined
                : translation("filesystemProtectionUnavailable"),
              control: (
                <SandboxToggleControl
                  enforce={sandboxEnforce}
                  backend={sandboxBackend.backend}
                  onChange={setSandboxEnforce}
                />
              ),
            },
            {
              key: "gitWorktree",
              title: translation("gitWorktree"),
              control: (
                <WorktreeStrategyControl value={worktreeStrategy} onChange={setWorktreeStrategy} />
              ),
            },
            {
              key: "compaction",
              title: translation("compaction"),
              control: (
                <CompactionToggleControl enabled={autoCompaction} onChange={setAutoCompaction} />
              ),
            },
            {
              key: "userContext",
              title: translation("userContext"),
              description: translation("userContextHint"),
              control: (
                <UserContextToggleControl
                  enabled={userContextEnabled}
                  onChange={setUserContextEnabled}
                />
              ),
            },
            {
              key: "computerControl",
              title: translation("computerControl"),
              description: translation("computerControlHint"),
              control: (
                <ComputerControlToggleControl
                  enabled={computerControlEnabled}
                  onChange={accessibilityGranted ? setComputerControlEnabled : undefined}
                />
              ),
            },
            {
              key: "toolbox",
              title: translation("toolbox"),
              description: toolboxAvailable
                ? translation("toolboxHint")
                : translation("toolboxUnavailable"),
              control: (
                <ToolboxToggleControl
                  enabled={toolboxEnabled && toolboxAvailable}
                  onChange={toolboxAvailable ? setToolboxEnabled : undefined}
                />
              ),
            },
            {
              key: "dictation",
              title: translation("dictation"),
              description: translation("dictationHint"),
              control: (
                <DictationToggleControl enabled={dictationEnabled} onChange={setDictationEnabled} />
              ),
            },
          ],
          block: grantAlerts,
        },
        {
          title: translation("appearance"),
          rows: [
            {
              key: "theme",
              title: translation("theme.label"),
              control: (
                <Box w="200px">
                  <SimpleSelect
                    items={[
                      { value: "system", label: translation("theme.system") },
                      { value: "light", label: translation("theme.light") },
                      { value: "dark", label: translation("theme.dark") },
                    ]}
                    value={appTheme}
                    onValueChange={(next) => setAppTheme(next as "system" | "light" | "dark")}
                  />
                </Box>
              ),
            },
            {
              key: "language",
              title: translation("language.label"),
              control: (
                <Box w="200px">
                  <SimpleSelect
                    items={LOCALES.map((code) => ({
                      value: code,
                      label: translation(`language.${code}`),
                    }))}
                    value={locale}
                    onValueChange={(next) => setLocale(next as Locale)}
                  />
                </Box>
              ),
            },
          ],
        },
        {
          title: translation("apiKeys"),
          rows: [
            secretRow(translation("exaApiKey"), "xxxxxxxx-...", exaApiKey, setExaApiKey),
            secretRow(translation("composioApiKey"), "cmp_...", composioApiKey, setComposioApiKey),
            secretRow(
              translation("jinaApiKey"),
              "jina_...",
              jinaApiKey,
              setJinaApiKey,
              translation("jinaApiKeyHint"),
            ),
            secretRow(
              translation("firecrawlApiKey"),
              "fc-...",
              firecrawlApiKey,
              setFirecrawlApiKey,
              translation("firecrawlApiKeyHint"),
            ),
            secretRow(
              translation("proxyServer"),
              "http://user:pass@host:port",
              webFetchProxyUrl,
              setWebFetchProxyUrl,
              translation("proxyServerHint"),
            ),
          ],
        },
        {
          title: translation("modelProviders"),
          rows: [],
          block: (
            <Flex direction="column" gap={5} w="100%">
              <ChatGPTAuthControl />
              <CursorAuthControl />
            </Flex>
          ),
        },
      ],
    },
    {
      id: "connection" as SettingsSection,
      label: translation("tabConnection"),
      icon: <LuMonitor size={14} />,
      sections: [{ title: translation("tabConnection"), rows: [], block: <MachinesPanel /> }],
    },
    ...(workspaceId
      ? [
          {
            id: "locations" as SettingsSection,
            label: translation("tabLocations"),
            icon: <LuServer size={14} />,
            sections: [
              {
                title: translation("tabLocations"),
                rows: [],
                block: <WorkspaceLocationsPanel workspaceId={workspaceId} />,
              },
            ],
          },
        ]
      : []),
    // Only with a workspace, like the environments tab, since a schedule belongs to one.
    ...(workspaceId
      ? [
          {
            id: "schedules" as SettingsSection,
            label: translation("tabSchedules"),
            icon: <LuClock size={14} />,
            sections: [
              {
                title: translation("tabSchedules"),
                rows: [],
                block: <SchedulesPanel workspaceId={workspaceId} agents={agents} />,
              },
            ],
          },
        ]
      : []),
    {
      id: "agents",
      label: translation("tabAgents"),
      icon: <LuUsers size={14} />,
      sections: [
        {
          title: translation("agent"),
          // The same picker the composer uses, because it picks the same thing: which agent profile runs.
          rows: [
            {
              key: "profile",
              title: translation("profile"),
              control: (
                <Box w="280px">
                  <AgentSelectControl
                    layout="field"
                    agents={agents}
                    value={settingsAgent}
                    onChange={setSettingsAgent}
                    placeholder={translation("chooseAgent")}
                  />
                </Box>
              ),
            },
          ],
          block: agentLoading ? (
            <Flex align="center" gap={2} color="fg.muted" fontSize="sm" py={3}>
              <Spinner size="xs" />
              {translation("loadingAgentConfiguration")}
            </Flex>
          ) : agentError ? (
            <Text fontSize="sm" color="red.fg">
              {agentError}
            </Text>
          ) : agentConfiguration ? (
            <AgentPermissionsEditor
              configuration={agentConfiguration}
              models={models}
              providers={modelProviders}
              recentModels={recentModels}
              onChange={setAgentConfiguration}
            />
          ) : null,
        },
        { title: "External agents", rows: [], block: <RemoteAgentsPanel /> },
      ],
    },
  ];

  // Everything else the configuration file holds, appended after the curated pages rather than merged into them.
  const pages = configurationPage ? [...curatedPages, configurationPage] : curatedPages;
  const activePage = pages.find((page) => page.id === section) ?? pages[0];
  const rowMatches = (row: SettingRowDef) =>
    `${row.title} ${row.description ?? ""}`.toLowerCase().includes(query);
  // When searching, collapse every page into just its matching rows, so results read as one flat list.
  const searchSections = searching
    ? pages.flatMap((page) =>
        page.sections
          .map((section) => ({
            ...section,
            rows: section.rows.filter(rowMatches),
            block: undefined,
          }))
          .filter((section) => section.rows.length > 0),
      )
    : [];

  return (
    <>
      <Dialog.Root
        open={open}
        onOpenChange={(event) => (event.open ? onOpenChange(true) : requestClose())}
        placement="center"
      >
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content
              data-layout="settings-dialog"
              // Full-bleed on a narrow screen and a card when wide, since a nearly full-viewport dialog reads as one that failed to fit.
              w={{ base: "100%", md: "min(900px, calc(100vw - 48px))" }}
              maxW={{ base: "100%", md: "900px" }}
              h={{ base: "100dvh", md: "min(760px, calc(100vh - 48px))" }}
              display="flex"
              flexDirection={{ base: "column", md: "row" }}
              overflow="hidden"
              p={0}
            >
              {/* The left nav spans the dialog's full height, beside the column carrying the header, content and footer. */}
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
                <Box px={{ base: 4, md: 3 }} py={3} flexShrink={0}>
                  <Flex
                    align="center"
                    gap={2}
                    h={8}
                    px={2}
                    borderRadius="md"
                    bg="bg"
                    borderWidth="1px"
                    borderColor="border.muted"
                    _focusWithin={{ borderColor: "border.emphasized" }}
                  >
                    <Box color="fg.muted" flexShrink={0} display="flex" alignItems="center">
                      <LuSearch size={14} />
                    </Box>
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
                <Box flex={1} minH={0} overflowY="auto" px={{ base: 4, md: 3 }} pb={3}>
                  <Text pb={2} textStyle="sectionLabel">
                    {translation("title")}
                  </Text>
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
                          onClick={() => {
                            setSettingsSearch("");
                            onSectionChange(page.id);
                          }}
                        >
                          <Box
                            color={active ? "fg" : "fg.subtle"}
                            flexShrink={0}
                            display="flex"
                            alignItems="center"
                          >
                            {page.icon}
                          </Box>
                          <Text flex={1} minW={0} truncate fontSize="xs">
                            {page.label}
                          </Text>
                        </Button>
                      );
                    })}
                  </Flex>
                </Box>
              </Flex>
              <Flex data-layout="settings-content" direction="column" flex={1} minW={0} minH={0}>
                <Dialog.Body px={0} py={2} flex={1} minH={0}>
                  {/* Right content: search results across all sections, or the active page's sections. */}
                  {/* One horizontal inset for the whole column, so the panel's edge does not move as you scroll. */}
                  <Box
                    ref={contentScrollRef}
                    onScroll={onContentScroll}
                    css={contentFade}
                    h="100%"
                    overflowY="auto"
                    px={{ base: 4, md: 6 }}
                    py={4}
                  >
                    {searching ? (
                      searchSections.length === 0 ? (
                        <Flex h="full" align="center" justify="center" py={10}>
                          <EmptyState.Root size="sm">
                            <EmptyState.Content>
                              <EmptyState.Indicator>
                                <LuSearch />
                              </EmptyState.Indicator>
                              <VStack gap={0}>
                                <EmptyState.Title fontSize="sm">
                                  {translation("searchNoResultsTitle")}
                                </EmptyState.Title>
                                <EmptyState.Description fontSize="xs">
                                  {translation("searchNoResults", { query: settingsSearch })}
                                </EmptyState.Description>
                              </VStack>
                            </EmptyState.Content>
                          </EmptyState.Root>
                        </Flex>
                      ) : (
                        searchSections.map((section, index) => (
                          <SettingsSection key={index} title={section.title}>
                            {section.rows.map((row) => (
                              <SettingRow
                                key={row.key}
                                title={row.title}
                                description={row.description}
                                layout={row.layout}
                              >
                                {row.control}
                              </SettingRow>
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
                                <SettingRow
                                  key={row.key}
                                  title={row.title}
                                  description={row.description}
                                  layout={row.layout}
                                >
                                  {row.control}
                                </SettingRow>
                              ))}
                              {/* A block after rows is separated by top padding; a standalone block sits flush under the heading. */}
                              {pageSection.block ? (
                                // Settings owns the content width and every panel fills it, so the measure does not change between panes.
                                <Box pt={pageSection.rows.length > 0 ? 4 : 0} w="100%" maxW="640px">
                                  {pageSection.block}
                                </Box>
                              ) : null}
                            </SettingsSection>
                          ))}
                        </Box>
                      ))
                    )}
                  </Box>
                </Dialog.Body>
                {/* Footer sits inside the right column so the nav stays full height. */}
                <Dialog.Footer pt={3} px={{ base: 4, md: 6 }}>
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
        onConfirm={() =>
          void (async () => {
            // Two processes, two restarts: the daemon is the one macOS has to re-evaluate, and it is no longer this app's child.
            try {
              await restartDaemon();
            } catch {
              // A daemon that cannot be asked is one the user restarts themselves, but reloading the window is still right.
            }
            await restartApp();
          })()
        }
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

function SettingField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
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
  const rules = Object.entries(configuration.bash.permissions).sort(
    ([leftPattern], [rightPattern]) => leftPattern.localeCompare(rightPattern),
  );

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
    if (nextPattern.trim())
      nextPermissions[nextPattern.trim()] =
        configuration.bash.permissions[previousPattern] ?? "ask";
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
                value={
                  configuration.provider && configuration.model
                    ? `${configuration.provider}/${configuration.model}`
                    : ""
                }
                onChange={updateModel}
              />
            </Box>
          </SettingField>
          <SettingField label={translation("permissionMode")}>
            {/* The one picker that may answer none, since most cards mean to declare no ceiling at all. */}
            <PermissionModeControl
              value={configuration.permission_mode}
              unsetLabel={translation("permissionNoCeiling")}
              onChange={(permissionModeValue) =>
                updateConfiguration({ permission_mode: permissionModeValue })
              }
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
                onClick={() =>
                  updateBash({ background_allowed: !configuration.bash.background_allowed })
                }
              >
                {configuration.bash.background_allowed
                  ? translation("backgroundAllowed")
                  : translation("backgroundBlocked")}
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
            <Button
              variant="subtle"
              colorPalette="blue"
              justifyContent="flex-start"
              onClick={addRule}
            >
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
