"use client";

import {
  Box,
  Button,
  createListCollection,
  Dialog,
  Flex,
  IconButton,
  Input,
  Portal,
  Select,
  Span,
  Text,
} from "@chakra-ui/react";
import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { LuBot, LuCheck, LuChevronDown, LuChevronRight, LuEye, LuEyeOff, LuImage, LuPaperclip } from "react-icons/lu";
import {
  fetchSettings,
  saveSettings,
  type ModelOption,
  type ProviderOption,
  type RecentModel,
  type Settings,
} from "@/lib/api";
import { ChatGPTAuthControl } from "@/components/chatgpt-auth";
import { CursorAuthControl } from "@/components/cursor-auth";
import { swallowed } from "@/lib/swallowed";

interface ModelSelectProps {
  models: ModelOption[];
  providers: ProviderOption[];
  value: string;
  onChange: (modelId: string) => void;
  recent?: RecentModel[];
  // Optional display fallback for a controlled value that has not loaded yet.
  fallbackModelId?: string;
  compact?: boolean;
  responsiveCompact?: boolean;
}

// Sentinel option in the model dropdown that reveals the free-form Model ID
// field, for providers/models the catalog does not list yet.
const CUSTOM_MODEL = "__custom__";

interface ProviderItem {
  value: string;
  label: string;
}

interface ModelItem {
  value: string;
  label: string;
  // Whether the provider currently serves this model (has a key, or — for a
  // subscription provider — the plan includes it). Unavailable models are listed
  // but greyed and non-selectable.
  available: boolean;
}

// The capability icons for a model, from its models.dev flags: an image glyph for
// vision (image input), a paperclip for a model that takes file attachments but not
// images. A text-only model shows nothing. Reused by the picker rows and the
// current-model chip so capabilities read the same everywhere.
export function ModelCapabilityBadges({ model, size = 12 }: { model?: ModelOption | null; size?: number }) {
  const translation = useTranslations("ModelSelect");
  if (!model) return null;
  const badges: { key: string; icon: React.ReactNode; label: string }[] = [];
  if (model.vision) {
    badges.push({ key: "vision", icon: <LuImage size={size} />, label: translation("visionBadge") });
  } else if (model.attachment) {
    badges.push({ key: "attachment", icon: <LuPaperclip size={size} />, label: translation("attachmentBadge") });
  }
  if (badges.length === 0) return null;
  return (
    <Flex align="center" pl={0.5} gap={1} color="fg.subtle" flexShrink={0}>
      {badges.map((badge) => (
        <Span key={badge.key} display="flex" alignItems="center" title={badge.label}>
          {badge.icon}
        </Span>
      ))}
    </Flex>
  );
}

export function modelSupportsVision(models: ModelOption[], modelId: string): boolean {
  if (!modelId) return true;
  const model = models.find((candidate) => candidate.id === modelId);
  if (!model) return true;
  return !!model.vision;
}

function providerForModel(modelId: string, models: ModelOption[]): string {
  const known = models.find((model) => model.id === modelId)?.provider;
  if (known) return known;
  if (modelId.includes("/")) return modelId.split("/", 1)[0];
  return models[0]?.provider ?? "";
}

function suffixForModel(modelId: string): string {
  return modelId.includes("/") ? modelId.slice(modelId.indexOf("/") + 1) : modelId;
}

function displayModelName(modelId: string, models: ModelOption[]): string {
  if (!modelId) return "provider/model";
  return models.find((model) => model.id === modelId)?.name ?? modelId;
}

/**
 * Whether a model's display name is a fallback to its raw ID rather than a
 * proper human-readable label. When true, the frontend renders it in monospace
 * to signal "this is a technical identifier, not a display name."
 */
function modelNameIsFallbackId(modelId: string, models: ModelOption[]): boolean {
  const model = models.find((model) => model.id === modelId);
  if (!model) return true; // unknown model — treat as fallback, render monospace
  return model.name === suffixForModel(modelId);
}

function providerName(providerId: string, providers: ProviderOption[]): string {
  return providers.find((provider) => provider.id === providerId)?.name ?? providerId;
}

function providerPlaceholder(providerId: string): string {
  if (providerId === "anthropic") return "sk-ant-...";
  if (providerId === "openai") return "sk-...";
  if (providerId === "opencode") return "opencode_...";
  if (providerId === "openrouter") return "sk-or-...";
  if (providerId === "xai") return "xai-...";
  if (providerId === "deepseek") return "sk-...";
  if (providerId === "groq") return "gsk_...";
  return "...";
}

function keyByProvider(settings: Settings): Record<string, string> {
  return Object.fromEntries(
    Object.entries(settings.providers).map(([identifier, credential]) => [identifier, credential.api_key ?? ""])
  );
}

export function ModelSelect({ models, providers, value, onChange, recent = [], fallbackModelId = "", compact, responsiveCompact = false }: ModelSelectProps) {
  const translation = useTranslations("ModelSelect");
  const tc = useTranslations("Common");
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({});
  const [selectedProvider, setSelectedProvider] = useState(() => providerForModel(value, models));
  const [selectedModel, setSelectedModel] = useState(value);
  const [modelSuffix, setModelSuffix] = useState(() => suffixForModel(value));
  const [customMode, setCustomMode] = useState(() => !!value && !models.some((model) => model.id === value));
  // The endpoint for the user-declared "custom" OpenAI-compatible provider,
  // edited here (not in Settings) since it belongs with the model choice.
  const [customBaseUrl, setCustomBaseUrl] = useState("");
  const [saving, setSaving] = useState(false);

  const providerItems = useMemo<ProviderItem[]>(
    () => providers.map((provider) => ({ value: provider.id, label: provider.name })),
    [providers]
  );
  const providerCollection = useMemo(() => createListCollection({ items: providerItems }), [providerItems]);

  const recentIds = useMemo(() => new Set(recent.map((model) => model.id)), [recent]);
  const modelItems = useMemo<ModelItem[]>(() => {
    const providerModels = models
      .filter((model) => model.provider === selectedProvider)
      .sort((left, right) => {
        // Available models first (unavailable sink to the bottom, greyed), then
        // recently-used, then newest release first (undated sink last), name as
        // the final tiebreak.
        if (left.available !== right.available) return left.available ? -1 : 1;
        const leftRecent = recentIds.has(left.id);
        const rightRecent = recentIds.has(right.id);
        if (leftRecent !== rightRecent) return leftRecent ? -1 : 1;
        const byRelease = (right.release_date || "").localeCompare(left.release_date || "");
        if (byRelease !== 0) return byRelease;
        return left.name.localeCompare(right.name);
      });
    const items = providerModels.map((model) => ({ value: model.id, label: model.name, available: model.available }));
    items.push({ value: CUSTOM_MODEL, label: translation("selectUnlisted"), available: true });
    return items;
  }, [models, recentIds, selectedProvider, translation]);
  // Every model stays selectable (unavailable ones are only greyed as a hint) —
  // the Apply button, not the dropdown, is what gates on having a usable credential.
  const modelCollection = useMemo(() => createListCollection({ items: modelItems }), [modelItems]);

  // Default to the first *available* model so we never pre-select a greyed one.
  const firstKnownModel =
    modelItems.find((item) => item.value !== CUSTOM_MODEL && item.available)?.value ??
    modelItems.find((item) => item.value !== CUSTOM_MODEL)?.value ??
    "";
  const customModelItem = modelItems.find((item) => item.value === CUSTOM_MODEL) ?? null;
  // The user-declared custom provider has no catalog models: skip the model
  // dropdown entirely and go straight to a free-form model id plus an endpoint.
  const selectedProviderIsCustom = selectedProvider === "custom";
  // The two experimental subscription providers sign in over OAuth instead of taking an
  // API key, so each swaps the key field for its own sign-in control.
  const selectedProviderIsChatGPT = selectedProvider === "chatgpt";
  const selectedProviderIsCursor = selectedProvider === "cursor";
  const selectedProviderIsSubscription = selectedProviderIsChatGPT || selectedProviderIsCursor;
  const inCustomMode = customMode || selectedProviderIsCustom;
  const selectedModelIsInProvider = !!selectedModel && providerForModel(selectedModel, models) === selectedProvider;
  const typedModel = modelSuffix.trim() ? `${selectedProvider}/${modelSuffix.trim()}` : "";
  const activeSelectedModel = inCustomMode
    ? typedModel
    : selectedModelIsInProvider
      ? selectedModel
      : firstKnownModel;
  const effectiveModelId = value || fallbackModelId;
  const chipModelName = effectiveModelId ? displayModelName(effectiveModelId, models) : translation("modelFallback");
  const chipNameIsFallback = effectiveModelId ? modelNameIsFallbackId(effectiveModelId, models) : true;
  const chipProviderLabel = effectiveModelId ? providerName(providerForModel(effectiveModelId, models), providers) : "";
  const chipModel = effectiveModelId ? (models.find((model) => model.id === effectiveModelId) ?? null) : null;
  const selectedProviderLabel = providerName(selectedProvider, providers);
  const selectedProviderKey = providerKeys[selectedProvider] ?? "";

  // Whether Apply is allowed: a model is picked AND its provider can actually serve
  // it. Rather than blocking selection, we let any model be chosen and only enable
  // Apply once the credential is in hand — a key typed here, an already-unlocked
  // provider (stored/env key), the keyless custom endpoint, or, for a subscription
  // provider, a signed-in plan that includes the specific model.
  const activeModelOption = models.find((model) => model.id === activeSelectedModel);
  const providerUnlocked = models.some((model) => model.provider === selectedProvider && model.available);
  const keyEntered = selectedProviderKey.trim().length > 0;
  const canApply = (() => {
    if (!activeSelectedModel) return false;
    if (selectedProviderIsCustom) return true;
    if (selectedProviderIsSubscription) return !!activeModelOption?.available;
    if (activeModelOption) return activeModelOption.available || keyEntered;
    return providerUnlocked || keyEntered; // a typed model id on a keyed provider
  })();

  function openDialog() {
    const base = value || fallbackModelId;
    const provider = providerForModel(base, models);
    const known = models.some((model) => model.id === base);
    setSelectedProvider(provider);
    setCustomMode(!!base && !known);
    setSelectedModel(base || models.find((model) => model.provider === provider)?.id || "");
    setModelSuffix(suffixForModel(base || models.find((model) => model.provider === provider)?.id || ""));
    setOpen(true);
  }

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchSettings()
      .then((loaded) => {
        if (cancelled) return;
        setSettings(loaded);
        setProviderKeys(keyByProvider(loaded));
        setCustomBaseUrl(loaded.providers?.custom?.base_url ?? "");
      })
      .catch((caught) => swallowed({ component: "model-select", operation: "read the settings" }, caught));
    return () => {
      cancelled = true;
    };
  }, [open]);

  async function handleApply() {
    setSaving(true);
    try {
      if (settings) {
        await saveSettings({
          exa_api_key: settings.exa_api_key,
          composio_api_key: settings.composio_api_key,
          // A subscription provider has no API key — its credential is the OAuth
          // sign-in, saved out-of-band — so never write a provider key for one.
          provider_keys: selectedProviderIsSubscription
            ? {}
            : { [selectedProvider]: selectedProviderKey.trim() },
          provider_base_urls: selectedProviderIsCustom ? { custom: customBaseUrl.trim() } : {},
          worktree_strategy: settings.worktree_strategy,
        });
      }
      onChange(activeSelectedModel);
      setOpen(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <Button
        data-composer-model=""
        variant="outline"
        px={2}
        // The icon-to-label gap is stated rather than inherited: the button recipe's `xs`
        // size gives 4px, while the agent and permission chips beside it in the composer are
        // Select triggers at 6px. Left to the recipe, this one control sat a notch tighter
        // than its two neighbours in the same row.
        gap={1.5}
        bg="bg"
        borderColor="border"
        minW={compact ? "max-content" : undefined}
        maxW={responsiveCompact ? "none" : compact ? "220px" : "100%"}
        flexShrink={0}
        onClick={openDialog}
      >
        <LuBot size={compact ? 13 : 14} />
        {chipProviderLabel ? (
          <Span data-composer-model-label={responsiveCompact ? "" : undefined} display="flex" alignItems="center" minW={0}>
            <Span data-composer-model-provider={responsiveCompact ? "" : undefined} color="fg.muted" truncate>
              {chipProviderLabel}
            </Span>
            <Span data-composer-model-provider={responsiveCompact ? "" : undefined} color="fg.subtle" display="flex" alignItems="center" flexShrink={0}>
              <LuChevronRight size={compact ? 11 : 13} />
            </Span>
            <Span truncate fontFamily={chipNameIsFallback ? "var(--app-font-mono)" : undefined} fontSize={chipNameIsFallback ? "xs" : undefined}>
              {chipModelName}
            </Span>
          </Span>
        ) : (
          <Span data-composer-model-label={responsiveCompact ? "" : undefined} truncate fontFamily={chipNameIsFallback ? "var(--app-font-mono)" : undefined} fontSize={chipNameIsFallback ? "xs" : undefined}>
            {chipModelName}
          </Span>
        )}
        <Box data-composer-model-capabilities={responsiveCompact ? "" : undefined} display="flex" flexShrink={0}>
          <ModelCapabilityBadges model={chipModel} size={compact ? 11 : 13} />
        </Box>
        <LuChevronDown size={compact ? 13 : 15} />
      </Button>

      <Dialog.Root open={open} onOpenChange={(event) => setOpen(event.open)} placement="center">
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content maxW="520px">
              <Dialog.Header>
                <Dialog.Title fontSize="sm">{translation("dialogTitle")}</Dialog.Title>
              </Dialog.Header>
              <Dialog.Body>
                <Text fontSize="xs" color="fg.muted" mb={4}>
                  {translation("dialogDescription")}
                </Text>
                <Flex direction="column" gap={4}>
                  <Box>
                    <Text textStyle="fieldLabel" mb={1}>
                      {translation("provider")}
                    </Text>
                    <Select.Root
                      collection={providerCollection}
                      value={selectedProvider ? [selectedProvider] : []}
                      onValueChange={(details) => {
                        const next = details.value[0];
                        if (next) {
                          setSelectedProvider(next);
                          setCustomMode(false);
                          const firstModel = models.find((model) => model.provider === next)?.id ?? "";
                          setSelectedModel(firstModel);
                          setModelSuffix(suffixForModel(firstModel));
                        }
                      }}
                      size="xs"
                    >
                      <Select.Control>
                        <Select.Trigger>
                          <Select.ValueText placeholder={translation("chooseProvider")} />
                        </Select.Trigger>
                        <Select.IndicatorGroup>
                          <Select.Indicator />
                        </Select.IndicatorGroup>
                      </Select.Control>
                      <Portal>
                        <Select.Positioner>
                          <Select.Content maxH="320px" overflowY="auto">
                            {providerItems.map((provider) => (
                              <Select.Item item={provider} key={provider.value}>
                                {provider.label}
                                <Select.ItemIndicator />
                              </Select.Item>
                            ))}
                          </Select.Content>
                        </Select.Positioner>
                      </Portal>
                    </Select.Root>
                  </Box>

                  {selectedProviderIsCustom ? null : (
                  <Box>
                    <Text textStyle="fieldLabel" mb={1}>
                      {translation("model")}
                    </Text>
                    <Select.Root
                      collection={modelCollection}
                      value={customMode ? [CUSTOM_MODEL] : activeSelectedModel ? [activeSelectedModel] : []}
                      onValueChange={(details) => {
                        const next = details.value[0];
                        if (!next) return;
                        if (next === CUSTOM_MODEL) {
                          setCustomMode(true);
                          setModelSuffix("");
                          return;
                        }
                        setCustomMode(false);
                        setSelectedModel(next);
                        setModelSuffix(suffixForModel(next));
                      }}
                      size="xs"
                    >
                      <Select.Control>
                        <Select.Trigger>
                          <Select.ValueText placeholder={translation("chooseModel")} />
                        </Select.Trigger>
                        <Select.IndicatorGroup>
                          <Select.Indicator />
                        </Select.IndicatorGroup>
                      </Select.Control>
                      <Portal>
                        <Select.Positioner>
                          <Select.Content maxH="320px" overflowY="auto">
                            {modelItems
                              .filter((model) => model.value !== CUSTOM_MODEL)
                              .map((model) => (
                                <Select.Item item={model} key={model.value} color={model.available ? undefined : "fg.subtle"}>
                                  <Flex align="center" gap={2} w="100%">
                                    {suffixForModel(model.value) !== model.label ? (
                                      <Text flex={1}>{model.label}</Text>
                                    ) : (
                                      <Text flex={1} fontFamily="var(--app-font-mono)" fontSize="xs">
                                        {model.label}
                                      </Text>
                                    )}
                                    <ModelCapabilityBadges model={models.find((candidate) => candidate.id === model.value) ?? null} />
                                    {model.available && recentIds.has(model.value) ? (
                                      <Text fontSize="xs" color="fg.subtle" flexShrink={0}>
                                        {translation("recent")}
                                      </Text>
                                    ) : null}
                                  </Flex>
                                  <Select.ItemIndicator />
                                </Select.Item>
                              ))}
                            {customModelItem ? (
                              <>
                                {/* Only divide from the model list when there is one. */}
                                {modelItems.some((model) => model.value !== CUSTOM_MODEL) ? (
                                  <Box borderTop="1px solid" borderColor="border" my={1} />
                                ) : null}
                                <Select.Item
                                  item={customModelItem}
                                  key={customModelItem.value}
                                  bg="blue.subtle"
                                  color="blue.fg"
                                  borderColor="border"
                                  _hover={{ bg: "blue.muted" }}
                                >
                                  {customModelItem.label}
                                  <Select.ItemIndicator />
                                </Select.Item>
                              </>
                            ) : null}
                          </Select.Content>
                        </Select.Positioner>
                      </Portal>
                    </Select.Root>
                  </Box>
                  )}

                  {inCustomMode ? (
                    <Box>
                      <Text textStyle="fieldLabel" mb={1}>
                        {translation("modelId")}
                      </Text>
                      <Input
                        fontFamily="var(--app-font-mono)"
                        fontSize="xs"
                        placeholder="model-name"
                        value={modelSuffix}
                        disabled={saving}
                        onChange={(event) => setModelSuffix(event.target.value)}
                      />
                      <Text fontSize="xs" color="fg.muted" mt={1.5}>
                        {translation.rich("sentToLitellm", {
                          model: `${selectedProvider}/${modelSuffix || "model-name"}`,
                          code: (chunks) => <Span fontFamily="var(--app-font-mono)">{chunks}</Span>,
                        })}
                      </Text>
                    </Box>
                  ) : null}

                  <Box>
                    {selectedProviderIsChatGPT ? (
                      <ChatGPTAuthControl />
                    ) : selectedProviderIsCursor ? (
                      <CursorAuthControl />
                    ) : (
                      <SecretField
                        label={translation("providerApiKey", { provider: selectedProviderLabel })}
                        placeholder={providerPlaceholder(selectedProvider)}
                        value={selectedProviderKey}
                        disabled={saving}
                        onChange={(next) => setProviderKeys((current) => ({ ...current, [selectedProvider]: next }))}
                      />
                    )}
                  </Box>

                  {selectedProviderIsCustom ? (
                    <Box>
                      <Text textStyle="fieldLabel" mb={1}>
                        {translation("endpoint")}
                      </Text>
                      <Input
                        fontFamily="var(--app-font-mono)"
                        fontSize="xs"
                        placeholder="https://api.example.com/v1"
                        value={customBaseUrl}
                        disabled={saving}
                        onChange={(event) => setCustomBaseUrl(event.target.value)}
                      />
                    </Box>
                  ) : null}
                </Flex>
              </Dialog.Body>
              <Dialog.Footer>
                <Button variant="outline" onClick={() => setOpen(false)} disabled={saving}>
                  {tc("cancel")}
                </Button>
                <Button colorPalette="blue" onClick={handleApply} loading={saving} disabled={!canApply}>
                  <LuCheck size={14} />
                  {translation("apply")}
                </Button>
              </Dialog.Footer>
              <Dialog.CloseTrigger />
            </Dialog.Content>
          </Dialog.Positioner>
        </Portal>
      </Dialog.Root>
    </>
  );
}

function SecretField({
  label,
  placeholder,
  value,
  disabled,
  onChange,
}: {
  label: string;
  placeholder: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const translation = useTranslations("ModelSelect");
  const [visible, setVisible] = useState(false);
  return (
    <Box>
      <Text textStyle="fieldLabel" mb={1}>
        {label}
      </Text>
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
