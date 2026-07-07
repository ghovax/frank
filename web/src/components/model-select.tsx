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
  Text,
} from "@chakra-ui/react";
import { useEffect, useMemo, useState } from "react";
import { LuBot, LuCheck, LuChevronDown, LuChevronRight, LuEye, LuEyeOff, LuImage, LuPaperclip } from "react-icons/lu";
import {
  fetchSettings,
  saveSettings,
  type ModelOption,
  type ProviderOption,
  type RecentModel,
  type Settings,
} from "@/lib/api";

interface ModelSelectProps {
  models: ModelOption[];
  providers: ProviderOption[];
  value: string;
  onChange: (modelId: string) => void;
  recent?: RecentModel[];
  // The globally-selected model, used to render the chip when `value` (a
  // per-conversation override) is empty — so it always names a real model.
  fallbackModelId?: string;
  compact?: boolean;
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
  curated: boolean;
}

// The capability icons for a model, from its models.dev flags: an image glyph for
// vision (image input), a paperclip for a model that takes file attachments but not
// images. A text-only model shows nothing. Reused by the picker rows and the
// composer's selected-model chip so capabilities read the same everywhere.
export function ModelCapabilityBadges({ model, size = 12 }: { model?: ModelOption | null; size?: number }) {
  if (!model) return null;
  const badges: { key: string; icon: React.ReactNode; label: string }[] = [];
  if (model.vision) {
    badges.push({ key: "vision", icon: <LuImage size={size} />, label: "Vision — accepts image input" });
  } else if (model.attachment) {
    badges.push({ key: "attachment", icon: <LuPaperclip size={size} />, label: "Accepts file attachments" });
  }
  if (badges.length === 0) return null;
  return (
    <Flex align="center" pl={0.5} gap={1} color="fg.subtle" flexShrink={0}>
      {badges.map((badge) => (
        <Box key={badge.key} as="span" display="flex" alignItems="center" title={badge.label}>
          {badge.icon}
        </Box>
      ))}
    </Flex>
  );
}

// Whether a model can accept file attachments. Unknown models (a typed/custom id
// not in the catalog) return true — we can't determine their capabilities, so we
// don't block. A known model without the attachment capability returns false.
export function modelSupportsAttachments(models: ModelOption[], modelId: string): boolean {
  if (!modelId) return true;
  const model = models.find((candidate) => candidate.id === modelId);
  if (!model) return true;
  return !!model.attachment;
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
  if (!modelId) return "Model";
  return models.find((model) => model.id === modelId)?.name ?? modelId;
}

/**
 * Whether a model's display name is a fallback to its raw ID rather than a
 * proper human-readable label. When true, the frontend renders it in monospace
 * to signal "this is a technical identifier, not a curated display name."
 */
function modelNameIsFallbackId(modelId: string, models: ModelOption[]): boolean {
  const model = models.find((m) => m.id === modelId);
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
    Object.entries(settings.providers ?? {}).map(([identifier, credential]) => [identifier, credential.api_key ?? ""])
  );
}

export function ModelSelect({ models, providers, value, onChange, recent = [], fallbackModelId = "", compact }: ModelSelectProps) {
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
        const leftRecent = recentIds.has(left.id);
        const rightRecent = recentIds.has(right.id);
        if (leftRecent !== rightRecent) return leftRecent ? -1 : 1;
        return left.name.localeCompare(right.name);
      });
    const items = providerModels.map((model) => ({ value: model.id, label: model.name, curated: model.curated }));
    items.push({ value: CUSTOM_MODEL, label: "Select unlisted third-party model...", curated: false });
    return items;
  }, [models, recentIds, selectedProvider]);
  const modelCollection = useMemo(() => createListCollection({ items: modelItems }), [modelItems]);

  const firstKnownModel = modelItems.find((item) => item.value !== CUSTOM_MODEL)?.value ?? "";
  const customModelItem = modelItems.find((item) => item.value === CUSTOM_MODEL) ?? null;
  // The user-declared custom provider has no catalog models: skip the model
  // dropdown entirely and go straight to a free-form model id plus an endpoint.
  const selectedProviderIsCustom = selectedProvider === "custom";
  const inCustomMode = customMode || selectedProviderIsCustom;
  const selectedModelIsInProvider = !!selectedModel && providerForModel(selectedModel, models) === selectedProvider;
  const typedModel = modelSuffix.trim() ? `${selectedProvider}/${modelSuffix.trim()}` : "";
  const activeSelectedModel = inCustomMode
    ? typedModel
    : selectedModelIsInProvider
      ? selectedModel
      : firstKnownModel;
  const effectiveModelId = value || fallbackModelId;
  const chipModelName = effectiveModelId ? displayModelName(effectiveModelId, models) : "Model";
  const chipNameIsFallback = effectiveModelId ? modelNameIsFallbackId(effectiveModelId, models) : true;
  const chipProviderLabel = effectiveModelId ? providerName(providerForModel(effectiveModelId, models), providers) : "";
  const chipModel = effectiveModelId ? (models.find((model) => model.id === effectiveModelId) ?? null) : null;
  const selectedProviderLabel = providerName(selectedProvider, providers);
  const selectedProviderKey = providerKeys[selectedProvider] ?? "";

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
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open]);

  async function handleApply() {
    setSaving(true);
    try {
      if (settings) {
        await saveSettings({
          exa_api_key: settings.exa_api_key ?? "",
          composio_api_key: settings.composio_api_key ?? "",
          provider_keys: {
            [selectedProvider]: selectedProviderKey.trim(),
          },
          provider_base_urls: selectedProviderIsCustom ? { custom: customBaseUrl.trim() } : {},
          // Persist the picked model as the global default in configuration.yaml,
          // so the choice survives a restart instead of only ever loading from it.
          selected_model: activeSelectedModel,
          workspace_strategy: settings.workspace_strategy ?? "none",
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
        size={compact ? "xs" : "sm"}
        variant="outline"
        borderRadius="sm"
        fontSize={compact ? "xs" : "sm"}
        h={compact ? "28px" : undefined}
        px={2}
        bg="bg"
        borderColor="border"
        minW={compact ? "max-content" : undefined}
        maxW={compact ? "220px" : "100%"}
        flexShrink={0}
        onClick={openDialog}
      >
        <LuBot size={compact ? 13 : 15} />
        {chipProviderLabel ? (
          <Flex as="span" align="center" minW={0}>
            <Box as="span" color="fg.muted" truncate>
              {chipProviderLabel}
            </Box>
            <Box as="span" color="fg.subtle" display="flex" alignItems="center" flexShrink={0}>
              <LuChevronRight size={compact ? 11 : 13} />
            </Box>
            <Box as="span" truncate fontFamily={chipNameIsFallback ? "var(--app-font-mono)" : undefined} fontSize={chipNameIsFallback ? "xs" : undefined}>
              {chipModelName}
            </Box>
          </Flex>
        ) : (
          <Box as="span" truncate fontFamily={chipNameIsFallback ? "var(--app-font-mono)" : undefined} fontSize={chipNameIsFallback ? "xs" : undefined}>
            {chipModelName}
          </Box>
        )}
        <ModelCapabilityBadges model={chipModel} size={compact ? 11 : 13} />
        <LuChevronDown size={compact ? 13 : 15} />
      </Button>

      <Dialog.Root open={open} onOpenChange={(event) => setOpen(event.open)} placement="center">
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content borderRadius="md" maxW="520px">
              <Dialog.Header>
                <Dialog.Title fontSize="sm">Configure model and provider</Dialog.Title>
              </Dialog.Header>
              <Dialog.Body>
                <Text fontSize="xs" color="fg.muted" mb={4}>
                  Choose the provider and model for this conversation, and the API key it
                  authenticates with. The selection applies immediately.
                </Text>
                <Flex direction="column" gap={4}>
                  <Box>
                    <Text fontSize="xs" fontWeight="medium" mb={1}>
                      Provider
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
                      size="sm"
                    >
                      <Select.Control>
                        <Select.Trigger borderRadius="sm">
                          <Select.ValueText placeholder="Choose provider" />
                        </Select.Trigger>
                        <Select.IndicatorGroup>
                          <Select.Indicator />
                        </Select.IndicatorGroup>
                      </Select.Control>
                      <Portal>
                        <Select.Positioner>
                          <Select.Content borderRadius="sm">
                            {providerItems.map((provider) => (
                              <Select.Item item={provider} key={provider.value} fontWeight="medium">
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
                    <Text fontSize="xs" fontWeight="medium" mb={1}>
                      Model
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
                      size="sm"
                    >
                      <Select.Control>
                        <Select.Trigger borderRadius="sm">
                          <Select.ValueText placeholder="Choose model" />
                        </Select.Trigger>
                        <Select.IndicatorGroup>
                          <Select.Indicator />
                        </Select.IndicatorGroup>
                      </Select.Control>
                      <Portal>
                        <Select.Positioner>
                          <Select.Content borderRadius="sm" maxH="300px" overflowY="auto">
                            {modelItems
                              .filter((model) => model.value !== CUSTOM_MODEL)
                              .map((model) => (
                                <Select.Item item={model} key={model.value} fontWeight="medium">
                                  <Flex align="center" gap={2} w="100%">
                                    {suffixForModel(model.value) !== model.label ? (
                                      <Text flex={1}>{model.label}</Text>
                                    ) : (
                                      <Text flex={1} fontFamily="var(--app-font-mono)" fontSize="xs">
                                        {model.label}
                                      </Text>
                                    )}
                                    <ModelCapabilityBadges model={models.find((candidate) => candidate.id === model.value) ?? null} />
                                    {recentIds.has(model.value) ? (
                                      <Text fontSize="xs" color="fg.subtle" flexShrink={0}>
                                        Recent
                                      </Text>
                                    ) : null}
                                  </Flex>
                                  <Select.ItemIndicator />
                                </Select.Item>
                              ))}
                            {customModelItem ? (
                              <>
                                <Box borderTop="1px solid" borderColor="border" my={1.5} />
                                <Select.Item
                                  item={customModelItem}
                                  key={customModelItem.value}
                                  bg="blue.subtle"
                                  color="blue.fg"
                                  fontWeight="medium"
                                  borderColor="border"
                                  pt={1}
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
                      <Text fontSize="xs" fontWeight="medium" mb={1}>
                        Model ID
                      </Text>
                      <Input
                        size="sm"
                        fontFamily="var(--app-font-mono)"
                        fontSize="xs"
                        borderRadius="sm"
                        placeholder="model-name"
                        value={modelSuffix}
                        disabled={saving}
                        onChange={(event) => setModelSuffix(event.target.value)}
                      />
                      <Text fontSize="xs" color="fg.muted" mt={1.5}>
                        Sent to LiteLLM as{" "}
                        <Box as="span" fontFamily="var(--app-font-mono)">
                          {selectedProvider}/{modelSuffix || "model-name"}
                        </Box>
                        .
                      </Text>
                    </Box>
                  ) : null}

                  <Box>
                    <SecretField
                      label={`${selectedProviderLabel} API key`}
                      placeholder={providerPlaceholder(selectedProvider)}
                      value={selectedProviderKey}
                      disabled={saving}
                      onChange={(next) => setProviderKeys((current) => ({ ...current, [selectedProvider]: next }))}
                    />
                  </Box>

                  {selectedProviderIsCustom ? (
                    <Box>
                      <Text fontSize="xs" fontWeight="medium" mb={1}>
                        Endpoint
                      </Text>
                      <Input
                        size="sm"
                        fontFamily="var(--app-font-mono)"
                        fontSize="xs"
                        borderRadius="sm"
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
                <Button size="sm" variant="outline" borderRadius="sm" onClick={() => setOpen(false)} disabled={saving}>
                  Cancel
                </Button>
                <Button size="sm" colorPalette="blue" borderRadius="sm" onClick={handleApply} loading={saving} disabled={!activeSelectedModel}>
                  <LuCheck size={14} />
                  Apply
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
  const [visible, setVisible] = useState(false);
  return (
    <Box>
      <Text fontSize="xs" fontWeight="medium" mb={1}>
        {label}
      </Text>
      <Flex gap={1.5} align="center">
        <Input
          size="sm"
          type={visible ? "text" : "password"}
          fontFamily="var(--app-font-mono)"
          fontSize="xs"
          borderRadius="sm"
          placeholder={placeholder}
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
        <IconButton
          aria-label={visible ? "Hide" : "Show"}
          size="sm"
          variant="ghost"
          borderRadius="sm"
          flexShrink={0}
          onClick={() => setVisible((current) => !current)}
        >
          {visible ? <LuEyeOff size={14} /> : <LuEye size={14} />}
        </IconButton>
      </Flex>
    </Box>
  );
}
