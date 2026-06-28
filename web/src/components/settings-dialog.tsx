"use client";

import { Box, Button, Dialog, Flex, IconButton, Input, Portal, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { LuEye, LuEyeOff, LuShieldCheck, LuShieldOff } from "react-icons/lu";
import { fetchSettings, saveSettings } from "@/lib/api";

// A dialog for entering the API credentials persisted in ~/.harness/configuration.yaml.
// Saving applies the keys live on the server (no restart needed).
export function SettingsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [apiKey, setApiKey] = useState("");
  const [exaApiKey, setExaApiKey] = useState("");
  const [sandboxEnabled, setSandboxEnabled] = useState(true);
  const [saving, setSaving] = useState(false);

  // Pre-fill from the server each time the dialog opens.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchSettings()
      .then((settings) => {
        if (cancelled) return;
        setApiKey(settings.api_key ?? "");
        setExaApiKey(settings.exa_api_key ?? "");
        setSandboxEnabled(settings.sandbox_enabled ?? true);
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
        api_key: apiKey.trim(),
        exa_api_key: exaApiKey.trim(),
        sandbox_enabled: sandboxEnabled,
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
          <Dialog.Content borderRadius="md" maxW="440px">
            <Dialog.Header>
              <Dialog.Title fontSize="sm">Settings</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Text fontSize="xs" color="fg.muted" mb={4}>
                API credentials are stored in <Box as="span" fontFamily="var(--app-font-mono)">~/.harness/configuration.yaml</Box> and applied immediately.
              </Text>
              <Flex direction="column" gap={4}>
                <SecretField
                  label="OpenCode API key"
                  placeholder="sk-..."
                  value={apiKey}
                  disabled={saving}
                  onChange={setApiKey}
                />
                <SecretField
                  label="Exa API key"
                  placeholder="xxxxxxxx-..."
                  value={exaApiKey}
                  disabled={saving}
                  onChange={setExaApiKey}
                />
                <SandboxToggle
                  enabled={sandboxEnabled}
                  disabled={saving}
                  onChange={setSandboxEnabled}
                />
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

function SandboxToggle({
  enabled,
  disabled,
  onChange,
}: {
  enabled: boolean;
  disabled: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <Box
      as="label"
      display="flex"
      alignItems="center"
      justifyContent="space-between"
      gap={3}
      border="1px solid"
      borderColor="border"
      borderRadius="sm"
      px={3}
      py={2}
      cursor={disabled ? "not-allowed" : "pointer"}
      opacity={disabled ? 0.65 : 1}
    >
      <Flex align="center" gap={2} minW={0}>
        <Box color={enabled ? "green.fg" : "fg.muted"} flexShrink={0}>
          {enabled ? <LuShieldCheck size={16} /> : <LuShieldOff size={16} />}
        </Box>
        <Box minW={0}>
          <Text fontSize="xs" fontWeight="medium">Sandbox</Text>
          <Text fontSize="xs" color="fg.muted">
            Ask before commands read outside the working directory.
          </Text>
        </Box>
      </Flex>
      <input
        type="checkbox"
        checked={enabled}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
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
      <Text fontSize="xs" fontWeight="medium" mb={1}>{label}</Text>
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
