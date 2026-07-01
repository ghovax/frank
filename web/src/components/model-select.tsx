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
import { LuBot, LuCheck, LuChevronDown, LuChevronRight, LuEye, LuEyeOff } from "react-icons/lu";
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
    const items = providerModels.map((model) => ({ value: model.id, label: model.name }));
    items.push({ value: CUSTOM_MODEL, label: "Other model" });
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
  const chipProviderLabel = effectiveModelId ? providerName(providerForModel(effectiveModelId, models), providers) : "";
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
          selected_model: settings.selected_model ?? "",
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
          <Flex as="span" align="center" gap={1.5} minW={0}>
            <Box as="span" color="fg.muted" truncate>
              {chipProviderLabel}
            </Box>
            <Box as="span" color="fg.subtle" display="flex" alignItems="center" flexShrink={0}>
              <LuChevronRight size={compact ? 11 : 13} />
            </Box>
            <Box as="span" truncate>
              {chipModelName}
            </Box>
          </Flex>
        ) : (
          <Box as="span" truncate>
            {chipModelName}
          </Box>
        )}
        <LuChevronDown size={compact ? 13 : 15} />
      </Button>

      <Dialog.Root open={open} onOpenChange={(event) => setOpen(event.open)} placement="center">
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content borderRadius="md" maxW="520px">
              <Dialog.Header>
                <Dialog.Title fontSize="sm">Model</Dialog.Title>
              </Dialog.Header>
              <Dialog.Body>
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
                                    <Text flex={1}>{model.label}</Text>
                                    {recentIds.has(model.value) ? (
                                      <Text fontSize="2xs" color="fg.subtle" flexShrink={0}>
                                        Recent
                                      </Text>
                                    ) : null}
                                  </Flex>
                                  <Select.ItemIndicator />
                                </Select.Item>
                              ))}
                            {customModelItem ? (
                              <Select.Item
                                item={customModelItem}
                                key={customModelItem.value}
                                bg="blue.subtle"
                                color="blue.fg"
                                fontWeight="medium"
                                borderTop="1px solid"
                                borderColor="border"
                                mt={1}
                                _hover={{ bg: "blue.muted" }}
                              >
                                {customModelItem.label}
                                <Select.ItemIndicator />
                              </Select.Item>
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
                        Sent to LiteLLM as {selectedProvider}/{modelSuffix || "model-name"}.
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
