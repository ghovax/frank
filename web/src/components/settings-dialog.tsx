"use client";

import { Box, Button, createListCollection, Dialog, Flex, IconButton, Input, Portal, Select, Text } from "@chakra-ui/react";
import { useEffect, useMemo, useState } from "react";
import { LuEye, LuEyeOff } from "react-icons/lu";
import { fetchSettings, saveSettings, type DotsOCRSettings } from "@/lib/api";

const DEFAULT_DOTS_OCR: DotsOCRSettings = {
  enabled: false,
  mode: "local",
  endpoint: "",
  api_key: "",
  model_name: "rednote-hilab/dots.mocr",
  prompt_mode: "prompt_layout_all_en",
  timeout_seconds: 900,
};

// A dialog for entering API credentials and choosing the selected model, persisted
// in ~/.daisy/configuration.yaml. Saving applies everything live (no restart).
export function SettingsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  // The globally-selected model is not edited here (it lives on the composer's
  // model picker); we only track it so saving preserves it rather than wiping it.
  const [selectedModel, setSelectedModel] = useState("");
  const [workspaceStrategy, setWorkspaceStrategy] = useState<"none" | "branch" | "worktree">("none");
  const [exaApiKey, setExaApiKey] = useState("");
  const [composioApiKey, setComposioApiKey] = useState("");
  const [dotsOCR, setDotsOCR] = useState<DotsOCRSettings>(DEFAULT_DOTS_OCR);
  const [saving, setSaving] = useState(false);
  const dotsOCRModes = useMemo(
    () =>
      createListCollection({
        items: [
          { label: "Local endpoint", value: "local" },
          { label: "Remote endpoint", value: "remote" },
        ],
      }),
    []
  );
  const dotsOCRPromptModes = useMemo(
    () =>
      createListCollection({
        items: [
          { label: "Full layout and text", value: "prompt_layout_all_en" },
          { label: "Layout only", value: "prompt_layout_only_en" },
          { label: "OCR text", value: "prompt_ocr" },
          { label: "Bounding-box OCR", value: "prompt_grounding_ocr" },
          { label: "Web page parsing", value: "prompt_web_parsing" },
          { label: "Scene text spotting", value: "prompt_scene_spotting" },
          { label: "Image to SVG", value: "prompt_image_to_svg" },
          { label: "General visual QA", value: "prompt_general" },
        ],
      }),
    []
  );

  // Pre-fill from the server each time the dialog opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchSettings()
      .then((settings) => {
        if (cancelled) return;
        setSelectedModel(settings.selected_model ?? "");
        setWorkspaceStrategy(settings.workspace_strategy ?? "none");
        setExaApiKey(settings.exa_api_key ?? "");
        setComposioApiKey(settings.composio_api_key ?? "");
        setDotsOCR({ ...DEFAULT_DOTS_OCR, ...(settings.dots_ocr ?? {}) });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open]);

  async function handleSave() {
    setSaving(true);
    try {
      await saveSettings({
        exa_api_key: exaApiKey.trim(),
        composio_api_key: composioApiKey.trim(),
        dots_ocr: {
          ...dotsOCR,
          endpoint: dotsOCR.endpoint.trim(),
          api_key: dotsOCR.api_key.trim(),
          model_name: dotsOCR.model_name.trim() || "rednote-hilab/dots.mocr",
          prompt_mode: dotsOCR.prompt_mode.trim() || "prompt_layout_all_en",
          timeout_seconds: Number(dotsOCR.timeout_seconds) || 900,
        },
        provider_keys: {},
        provider_base_urls: {},
        selected_model: selectedModel,
        workspace_strategy: workspaceStrategy,
      });
      onOpenChange(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(event) => onOpenChange(event.open)} placement="center">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content borderRadius="md" maxW="480px">
            <Dialog.Header>
              <Dialog.Title fontSize="sm">Settings</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Text fontSize="xs" color="fg.muted" mb={4}>
                Credentials are stored in{" "}
                <Box as="span" fontFamily="var(--app-font-mono)">
                  ~/.daisy/configuration.yaml
                </Box>{" "}
                and applied immediately.
              </Text>
              <Flex direction="column" gap={4}>
                {/* overflowY:auto makes the browser clip overflow-x too, which cut off
                    the focus ring on the sides — inner padding (with a compensating
                    negative margin so the fields stay aligned) gives the ring room. */}
                <Box maxH="420px" overflowY="auto" px={2} py={1} mx={-2} display="flex" flexDir="column" gap={3}>
                  <SecretField
                    label="Exa API key"
                    placeholder="xxxxxxxx-..."
                    value={exaApiKey}
                    disabled={saving}
                    onChange={setExaApiKey}
                  />
                  <SecretField
                    label="Composio API key"
                    placeholder="cmp_..."
                    value={composioApiKey}
                    disabled={saving}
                    onChange={setComposioApiKey}
                  />
                  <Box borderTopWidth="1px" borderColor="border" pt={3} display="flex" flexDirection="column" gap={3}>
                    <label>
                      <Flex align="center" gap={2}>
                        <input
                          type="checkbox"
                          checked={dotsOCR.enabled}
                          disabled={saving}
                          onChange={(event) => setDotsOCR((current) => ({ ...current, enabled: event.currentTarget.checked }))}
                        />
                        <Text fontSize="xs" fontWeight="medium">
                          Enable Dots OCR
                        </Text>
                      </Flex>
                    </label>
                    <Box>
                      <Text fontSize="xs" fontWeight="medium" mb={1}>
                        Dots OCR mode
                      </Text>
                      <Select.Root
                        collection={dotsOCRModes}
                        value={[dotsOCR.mode]}
                        onValueChange={(details) =>
                          setDotsOCR((current) => ({ ...current, mode: (details.value[0] as "local" | "remote") ?? "local" }))
                        }
                        disabled={saving}
                      >
                        <Select.Control>
                          <Select.Trigger borderRadius="sm" minH="32px">
                            <Select.ValueText />
                          </Select.Trigger>
                          <Select.IndicatorGroup>
                            <Select.Indicator />
                          </Select.IndicatorGroup>
                        </Select.Control>
                        <Select.Positioner>
                          <Select.Content borderRadius="sm">
                            {dotsOCRModes.items.map((item) => (
                              <Select.Item item={item} key={item.value} fontWeight="medium">
                                {item.label}
                                <Select.ItemIndicator />
                              </Select.Item>
                            ))}
                          </Select.Content>
                        </Select.Positioner>
                      </Select.Root>
                    </Box>
                    <PlainField
                      label="Dots OCR endpoint"
                      placeholder="http://127.0.0.1:8765/parse"
                      value={dotsOCR.endpoint}
                      disabled={saving}
                      onChange={(value) => setDotsOCR((current) => ({ ...current, endpoint: value }))}
                    />
                    <SecretField
                      label="Dots OCR API key"
                      placeholder="optional"
                      value={dotsOCR.api_key}
                      disabled={saving}
                      onChange={(value) => setDotsOCR((current) => ({ ...current, api_key: value }))}
                    />
                    <Flex gap={2}>
                      <PlainField
                        label="Model name"
                        placeholder="rednote-hilab/dots.mocr"
                        value={dotsOCR.model_name}
                        disabled={saving}
                        onChange={(value) => setDotsOCR((current) => ({ ...current, model_name: value }))}
                      />
                      <PlainField
                        label="Timeout seconds"
                        placeholder="900"
                        value={String(dotsOCR.timeout_seconds)}
                        disabled={saving}
                        onChange={(value) => setDotsOCR((current) => ({ ...current, timeout_seconds: Number(value) || 0 }))}
                      />
                    </Flex>
                    <Box>
                      <Text fontSize="xs" fontWeight="medium" mb={1}>
                        Prompt mode
                      </Text>
                      <Select.Root
                        collection={dotsOCRPromptModes}
                        value={[dotsOCR.prompt_mode]}
                        onValueChange={(details) =>
                          setDotsOCR((current) => ({ ...current, prompt_mode: details.value[0] ?? "prompt_layout_all_en" }))
                        }
                        disabled={saving}
                      >
                        <Select.Control>
                          <Select.Trigger borderRadius="sm" minH="32px">
                            <Select.ValueText />
                          </Select.Trigger>
                          <Select.IndicatorGroup>
                            <Select.Indicator />
                          </Select.IndicatorGroup>
                        </Select.Control>
                        <Select.Positioner>
                          <Select.Content borderRadius="sm">
                            {dotsOCRPromptModes.items.map((item) => (
                              <Select.Item item={item} key={item.value} fontWeight="medium">
                                {item.label}
                                <Select.ItemIndicator />
                              </Select.Item>
                            ))}
                          </Select.Content>
                        </Select.Positioner>
                      </Select.Root>
                    </Box>
                  </Box>
                </Box>
              </Flex>
            </Dialog.Body>
            <Dialog.Footer>
              <Button size="sm" variant="outline" borderRadius="sm" onClick={() => onOpenChange(false)} disabled={saving}>
                Cancel
              </Button>
              <Button size="sm" colorPalette="blue" borderRadius="sm" onClick={handleSave} loading={saving}>
                Save
              </Button>
            </Dialog.Footer>
            <Dialog.CloseTrigger />
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

function PlainField({
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
  return (
    <Box flex="1">
      <Text fontSize="xs" fontWeight="medium" mb={1}>
        {label}
      </Text>
      <Input
        size="sm"
        fontFamily="var(--app-font-mono)"
        fontSize="xs"
        borderRadius="sm"
        placeholder={placeholder}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    </Box>
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
