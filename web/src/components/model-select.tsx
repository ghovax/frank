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
import { LuBot, LuCheck, LuChevronDown, LuEye, LuEyeOff } from "react-icons/lu";
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
  clearLabel?: string;
  compact?: boolean;
}

interface ProviderItem {
  value: string;
  label: string;
}

interface ModelItem {
  value: string;
  label: string;
}

function providerForModel(modelId: string, models: ModelOption[]): string {
  return models.find((model) => model.id === modelId)?.provider ?? models[0]?.provider ?? "";
}

function displayModelName(modelId: string, models: ModelOption[], clearLabel?: string): string {
  if (!modelId) return clearLabel ?? "Default model";
  return models.find((model) => model.id === modelId)?.name ?? modelId;
}

function providerName(providerId: string, providers: ProviderOption[]): string {
  return providers.find((provider) => provider.id === providerId)?.name ?? providerId;
}

function providerPlaceholder(providerId: string): string {
  if (providerId === "anthropic") return "sk-ant-...";
  if (providerId === "openai") return "sk-...";
  if (providerId === "opencode") return "OpenCode key";
  return "...";
}

function keyByProvider(settings: Settings): Record<string, string> {
  return Object.fromEntries(
    Object.entries(settings.providers ?? {}).map(([identifier, credential]) => [identifier, credential.api_key ?? ""])
  );
}

function baseUrlByProvider(settings: Settings): Record<string, string> {
  return Object.fromEntries(
    Object.entries(settings.providers ?? {}).map(([identifier, credential]) => [identifier, credential.base_url ?? ""])
  );
}

export function ModelSelect({ models, providers, value, onChange, recent = [], clearLabel, compact }: ModelSelectProps) {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({});
  const [providerBaseUrls, setProviderBaseUrls] = useState<Record<string, string>>({});
  const [selectedProvider, setSelectedProvider] = useState(() => providerForModel(value, models));
  const [selectedModel, setSelectedModel] = useState(value);
  const [saving, setSaving] = useState(false);

  const providersWithModels = useMemo(
    () => providers.filter((provider) => models.some((model) => model.provider === provider.id)),
    [providers, models]
  );
  const selectedProviderConfig = providers.find((provider) => provider.id === selectedProvider);
  const providerItems = useMemo<ProviderItem[]>(
    () => providersWithModels.map((provider) => ({ value: provider.id, label: provider.name })),
    [providersWithModels]
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
    return providerModels.map((model) => ({ value: model.id, label: model.name }));
  }, [models, recentIds, selectedProvider]);
  const modelCollection = useMemo(() => createListCollection({ items: modelItems }), [modelItems]);

  const selectedModelIsInProvider = !!selectedModel && models.find((model) => model.id === selectedModel)?.provider === selectedProvider;
  const activeSelectedModel = selectedModelIsInProvider ? selectedModel : (modelItems[0]?.value ?? "");
  const label = displayModelName(value, models, clearLabel);
  const selectedProviderLabel = providerName(selectedProvider, providers);
  const selectedProviderKey = providerKeys[selectedProvider] ?? "";
  const selectedProviderBaseUrl = providerBaseUrls[selectedProvider] ?? "";

  function openDialog() {
    const provider = providerForModel(value, models);
    setSelectedProvider(provider);
    setSelectedModel(value || models.find((model) => model.provider === provider)?.id || "");
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
        setProviderBaseUrls(baseUrlByProvider(loaded));
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
          composio_consumer_api_key: settings.composio_consumer_api_key ?? "",
          provider_keys: {
            [selectedProvider]: selectedProviderKey.trim(),
          },
          provider_base_urls: selectedProviderConfig?.openai_compatible
            ? { [selectedProvider]: selectedProviderBaseUrl.trim() }
            : {},
          default_model: settings.default_model ?? "",
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
        <Box as="span" truncate>
          {label}
        </Box>
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
                          setSelectedModel(models.find((model) => model.provider === next)?.id ?? "");
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

                  <Box>
                    <Text fontSize="xs" fontWeight="medium" mb={1}>
                      Model
                    </Text>
                    <Select.Root
                      collection={modelCollection}
                      value={activeSelectedModel ? [activeSelectedModel] : []}
                      onValueChange={(details) => {
                        if (details.value[0]) setSelectedModel(details.value[0]);
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
                            {modelItems.map((model) => (
                              <Select.Item item={model} key={model.value}>
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
                          </Select.Content>
                        </Select.Positioner>
                      </Portal>
                    </Select.Root>
                  </Box>

                  <Box>
                    <SecretField
                      label={`${selectedProviderLabel} API key`}
                      placeholder={providerPlaceholder(selectedProvider)}
                      value={selectedProviderKey}
                      disabled={saving}
                      onChange={(next) => setProviderKeys((current) => ({ ...current, [selectedProvider]: next }))}
                    />
                    {selectedProviderConfig?.openai_compatible ? (
                      <Box mt={2}>
                        <Text fontSize="xs" fontWeight="medium" mb={1} color="fg.muted">
                          Base URL
                        </Text>
                        <Input
                          size="sm"
                          fontFamily="var(--app-font-mono)"
                          fontSize="xs"
                          borderRadius="sm"
                          placeholder="https://..."
                          value={selectedProviderBaseUrl}
                          disabled={saving}
                          onChange={(event) =>
                            setProviderBaseUrls((current) => ({ ...current, [selectedProvider]: event.target.value }))
                          }
                        />
                      </Box>
                    ) : null}
                  </Box>
                </Flex>
              </Dialog.Body>
              <Dialog.Footer>
                {clearLabel ? (
                  <Button size="sm" variant="ghost" borderRadius="sm" mr="auto" onClick={() => { onChange(""); setOpen(false); }}>
                    {clearLabel}
                  </Button>
                ) : null}
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
