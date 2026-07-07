"use client";

import { Box, Button, Dialog, Flex, IconButton, Input, Portal, Tabs, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { LuEye, LuEyeOff, LuKeyRound, LuPlug } from "react-icons/lu";
import { fetchSettings, saveSettings } from "@/lib/api";
import type { ConnectionTarget } from "@/lib/connection";
import { ConnectionSettings } from "./connection-settings";

export type SettingsSection = "general" | "connection";

// A dialog for entering API credentials and choosing the selected model, persisted
// in ~/.daisy/configuration.yaml. Saving applies everything live (no restart).
export function SettingsDialog({
  open,
  onOpenChange,
  section,
  onSectionChange,
  currentConnectionId,
  onConnectionChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  currentConnectionId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
}) {
  // The globally-selected model is not edited here (it lives on the composer's
  // model picker); we only track it so saving preserves it rather than wiping it.
  const [selectedModel, setSelectedModel] = useState("");
  const [workspaceStrategy, setWorkspaceStrategy] = useState<"none" | "branch" | "worktree">("none");
  const [exaApiKey, setExaApiKey] = useState("");
  const [savedExaApiKey, setSavedExaApiKey] = useState("");
  const [composioApiKey, setComposioApiKey] = useState("");
  const [savedComposioApiKey, setSavedComposioApiKey] = useState("");
  const [connectionDirty, setConnectionDirty] = useState(false);
  const [connectionResetToken, setConnectionResetToken] = useState(0);
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const generalDirty = exaApiKey !== savedExaApiKey || composioApiKey !== savedComposioApiKey;
  const hasUnsavedChanges = generalDirty || connectionDirty;

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
        setSavedExaApiKey(settings.exa_api_key ?? "");
        setComposioApiKey(settings.composio_api_key ?? "");
        setSavedComposioApiKey(settings.composio_api_key ?? "");
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open]);

  async function handleSave() {
    setSaving(true);
    try {
      const nextExaApiKey = exaApiKey.trim();
      const nextComposioApiKey = composioApiKey.trim();
      await saveSettings({
        exa_api_key: nextExaApiKey,
        composio_api_key: nextComposioApiKey,
        provider_keys: {},
        provider_base_urls: {},
        selected_model: selectedModel,
        workspace_strategy: workspaceStrategy,
      });
      setExaApiKey(nextExaApiKey);
      setSavedExaApiKey(nextExaApiKey);
      setComposioApiKey(nextComposioApiKey);
      setSavedComposioApiKey(nextComposioApiKey);
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
    setConnectionResetToken((current) => current + 1);
    setConnectionDirty(false);
    setDiscardConfirmOpen(false);
    onOpenChange(false);
  }

  return (
    <>
    <Dialog.Root open={open} onOpenChange={(event) => event.open ? onOpenChange(true) : requestClose()} placement="center">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content borderRadius="md" maxW="900px" h="min(760px, calc(100vh - 48px))" display="flex" flexDirection="column">
            <Dialog.Header>
              <Dialog.Title fontSize="sm">Settings</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body px={0} py={0} flex={1} minH={0}>
              <Tabs.Root
                value={section}
                onValueChange={(details) => onSectionChange(details.value as SettingsSection)}
                orientation="vertical"
                variant="subtle"
                display="flex"
                h="100%"
                minH={0}
              >
                <Tabs.List w="240px" borderRight="1px solid" borderColor="border" px={3} gap={1}>
                  <Tabs.Trigger value="general" justifyContent="flex-start" borderRadius="sm" _selected={{ bg: "bg.muted", color: "fg", shadow: "none" }}>
                    <LuKeyRound size={14} />
                    General
                  </Tabs.Trigger>
                  <Tabs.Trigger value="connection" justifyContent="flex-start" borderRadius="sm" _selected={{ bg: "bg.muted", color: "fg", shadow: "none" }}>
                    <LuPlug size={14} />
                    Connection
                  </Tabs.Trigger>
                </Tabs.List>
                <Box flex={1} minW={0} minH={0}>
                  <Tabs.Content value="general" pr={4} h="100%" overflowY="auto">
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
                      </Box>
                    </Flex>
                  </Tabs.Content>
                  <Tabs.Content value="connection" pr={4} h="100%" overflow="hidden">
                    <Box h="100%" overflowY="auto" px={2} py={1} mx={-2}>
                      <Text fontSize="xs" color="fg.muted" mb={4}>
                        Configure local, remote, and SSH-backed connections. SSH hosts are loaded from{" "}
                        <Box as="span" fontFamily="var(--app-font-mono)">
                          ~/.ssh/config
                        </Box>{" "}
                        and can be overridden before saving.
                      </Text>
                      <ConnectionSettings
                        key={connectionResetToken}
                        variant="dialog"
                        currentTargetId={currentConnectionId}
                        onDirtyChange={setConnectionDirty}
                        onConnected={(target) => {
                          onConnectionChange?.(target);
                          onOpenChange(false);
                        }}
                      />
                    </Box>
                  </Tabs.Content>
                </Box>
              </Tabs.Root>
            </Dialog.Body>
            <Dialog.Footer borderTop="1px solid" borderColor="border" pt={4} pb={4}>
              <Button size="sm" variant="outline" borderRadius="sm" onClick={requestClose} disabled={saving}>
                Close
              </Button>
              {section === "general" && (
                <Button size="sm" colorPalette="blue" borderRadius="sm" onClick={handleSave} loading={saving}>
                  Save
                </Button>
              )}
            </Dialog.Footer>
            <Dialog.CloseTrigger />
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
    <Dialog.Root open={discardConfirmOpen} onOpenChange={(event) => setDiscardConfirmOpen(event.open)} placement="center" role="alertdialog">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content borderRadius="md" maxW="420px">
            <Dialog.Header>
              <Dialog.Title fontSize="sm">Discard unsaved changes?</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body>
              <Text fontSize="sm" color="fg.muted">
                Some fields have unsaved or incomplete values. Closing settings will discard them.
              </Text>
            </Dialog.Body>
            <Dialog.Footer>
              <Button size="sm" variant="outline" borderRadius="sm" onClick={() => setDiscardConfirmOpen(false)}>
                Keep editing
              </Button>
              <Button size="sm" colorPalette="red" borderRadius="sm" onClick={discardChangesAndClose}>
                Discard
              </Button>
            </Dialog.Footer>
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
