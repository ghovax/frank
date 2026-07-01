"use client";

import { Box, Button, Dialog, Flex, IconButton, Input, Portal, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { LuEye, LuEyeOff } from "react-icons/lu";
import { fetchSettings, saveSettings } from "@/lib/api";

// A dialog for entering API credentials and choosing the selected model, persisted
// in ~/.daisy/configuration.yaml. Saving applies everything live (no restart).
export function SettingsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  // The globally-selected model is not edited here (it lives on the composer's
  // model picker); we only track it so saving preserves it rather than wiping it.
  const [selectedModel, setSelectedModel] = useState("");
  const [workspaceStrategy, setWorkspaceStrategy] = useState<"none" | "branch" | "worktree">("none");
  const [exaApiKey, setExaApiKey] = useState("");
  const [composioApiKey, setComposioApiKey] = useState("");
  const [saving, setSaving] = useState(false);

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
                <Box maxH="260px" overflowY="auto" pr={1} display="flex" flexDir="column" gap={3}>
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
