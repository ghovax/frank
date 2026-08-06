"use client";

import { Box, Button, Flex, IconButton, Input, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { LuPlus, LuRotateCcw, LuTrash2 } from "react-icons/lu";
import { SimpleSelect } from "./ui/simple-select";
import { Tooltip } from "./ui/tooltip";
import {
  PermissionModeControl,
  SandboxToggleControl,
  SettingToggleControl,
  WorktreeStrategyControl,
  type WorktreeStrategyValue,
} from "./session-controls";
import type { PermissionMode, SettingEntry } from "@/lib/api";

// One control per kind of setting, built from the same pieces every hand-written row in this panel is built from — the chip toggle, the 200px `SimpleSelect`, the monospace `Input`, the row of inputs with a trash button and an add beneath it — so a generated row and a curated one are the same object to a reader.

/** What a person is typing, which is theirs until they are finished with it — and which follows the stored value when *that* changes underneath: another window, the terminal, a reset. */
function useSyncedDraft(value: unknown): [string, (next: string) => void] {
  const text = value === null || value === undefined ? "" : String(value);
  const [draft, setDraft] = useState(text);
  const [seen, setSeen] = useState(text);
  if (seen !== text) {
    setSeen(text);
    setDraft(text);
  }
  return [draft, setDraft];
}

/** The action that puts one setting back to what the code ships. */
function ResetAction({ onReset, busy }: { onReset: () => void; busy: boolean }) {
  const translation = useTranslations("SettingsDialog");
  return (
    <Tooltip content={translation("resetSetting")} openDelay={300}>
      <IconButton aria-label={translation("resetSetting")} variant="ghost" flexShrink={0} onClick={onReset} disabled={busy}>
        <LuRotateCcw size={13} />
      </IconButton>
    </Tooltip>
  );
}

function NumberField({ entry, onChange, onReset, busy }: ControlProps) {
  const [draft, setDraft] = useSyncedDraft(entry.value);
  const commit = () => {
    const trimmed = draft.trim();
    // An emptied number is not zero and is not a null the schema would take: it is the person saying they no longer have an opinion, which is what putting it back means.
    if (!trimmed) return onReset();
    const parsed = entry.kind === "integer" ? Number.parseInt(trimmed, 10) : Number.parseFloat(trimmed);
    if (Number.isNaN(parsed)) return setDraft(String(entry.value ?? ""));
    if (parsed !== entry.value) onChange(parsed);
  };
  return (
    <Box w="140px">
      <Input
        fontFamily="var(--app-font-mono)"
        fontSize="xs"
        inputMode="decimal"
        value={draft}
        disabled={busy}
        placeholder={String(entry.default ?? "")}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }}
      />
    </Box>
  );
}

function TextField({ entry, onChange, onReset, busy }: ControlProps) {
  const [draft, setDraft] = useSyncedDraft(entry.value);
  const commit = () => {
    if (draft === String(entry.value ?? "")) return;
    // A field emptied by hand is put back rather than written as "", unless the schema says the field may genuinely hold nothing — for a credential, blank *is* the way to remove it.
    if (!draft && !entry.optional && !entry.secret) return onReset();
    onChange(draft);
  };
  return (
    <Input
      fontFamily="var(--app-font-mono)"
      fontSize="xs"
      // A credential is masked because the schema declares it one, not because of how it is spelled.
      type={entry.secret ? "password" : "text"}
      value={draft}
      disabled={busy}
      placeholder={String(entry.default ?? "")}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }}
    />
  );
}

/** A list of strings: one field per entry, a trash button each, and an add beneath — the same editor the agent's command rules use, because it is the same shape of thing. */
function ListField({ entry, onChange, busy }: ControlProps) {
  const translation = useTranslations("SettingsDialog");
  const values = Array.isArray(entry.value) ? (entry.value as unknown[]).map(String) : [];
  const write = (next: string[]) => onChange(next.map((item) => item.trim()).filter(Boolean));
  return (
    <Flex direction="column" gap={2} minW={0} w="100%">
      {values.map((value, index) => (
        <Flex key={index} gap={2} align="center" minW={0}>
          <Input
            fontFamily="var(--app-font-mono)"
            fontSize="xs"
            value={value}
            disabled={busy}
            onChange={(event) => write(values.map((item, position) => (position === index ? event.target.value : item)))}
          />
          <IconButton
            aria-label={translation("removeEntry")}
            variant="ghost"
            colorPalette="red"
            flexShrink={0}
            disabled={busy}
            onClick={() => write(values.filter((_, position) => position !== index))}
          >
            <LuTrash2 size={14} />
          </IconButton>
        </Flex>
      ))}
      <Button variant="subtle" colorPalette="blue" justifyContent="flex-start" disabled={busy} onClick={() => onChange([...values, ""])}>
        <LuPlus size={14} />
        {translation("addEntry")}
      </Button>
    </Flex>
  );
}

interface ControlProps {
  entry: SettingEntry;
  onChange: (value: unknown) => void;
  onReset: () => void;
  busy: boolean;
}

export function SettingControl({ entry, onChange, onReset, busy = false }: {
  entry: SettingEntry;
  onChange: (value: unknown) => void;
  onReset: () => void;
  busy?: boolean;
}) {
  const translation = useTranslations("SettingsDialog");
  // The words for the values a choice takes, keyed by `<path>.<value>`.
  const choiceLabel = useTranslations("SettingsChoices") as unknown as {
    (key: string): string;
    has: (key: string) => boolean;
  };
  const props: ControlProps = { entry, onChange, onReset, busy };
  const reset = entry.configured ? <ResetAction onReset={onReset} busy={busy} /> : null;

  if (entry.kind === "boolean") {
    return (
      <Flex align="center" gap={1.5} justify="flex-end">
        {reset}
        <SettingToggleControl enabled={entry.value === true} onChange={busy ? undefined : onChange} />
      </Flex>
    );
  }

  if (entry.kind === "choice") {
    // Three settings already have a control of their own, and they are better than a select: each states what the choice *means* rather than the word the file happens to store.
    const bespoke =
      entry.path === "agent.permission_mode" ? (
        <PermissionModeControl value={String(entry.value ?? "") as PermissionMode} onChange={(mode) => onChange(mode)} />
      ) : entry.path === "workspace.strategy" ? (
        <WorktreeStrategyControl value={String(entry.value ?? "") as WorktreeStrategyValue} onChange={onChange} />
      ) : entry.path === "sandbox.enforce" ? (
        <SandboxToggleControl enforce={String(entry.value ?? "") as "required" | "preferred" | "off"} onChange={onChange} />
      ) : null;
    return (
      <Flex align="center" gap={1.5} justify="flex-end">
        {reset}
        {bespoke ?? (
          <Box w="200px">
            <SimpleSelect
              items={entry.choices.map((choice) => ({
                value: choice,
                label: choiceLabel.has(`${entry.path}.${choice}`) ? choiceLabel(`${entry.path}.${choice}`) : choice,
              }))}
              value={String(entry.value ?? "")}
              onValueChange={onChange}
            />
          </Box>
        )}
      </Flex>
    );
  }

  if (entry.kind === "integer" || entry.kind === "number") {
    return (
      <Flex align="center" gap={1.5} justify="flex-end">
        {reset}
        <NumberField {...props} />
      </Flex>
    );
  }

  if (entry.kind === "list") {
    return (
      <Flex align="flex-start" gap={1.5} w="100%">
        <ListField {...props} />
        {reset}
      </Flex>
    );
  }

  if (entry.kind === "map") {
    // Named, and not edited here.
    const keys = Object.keys((entry.value as Record<string, unknown>) ?? {});
    return (
      <Text fontSize="xs" color="fg.subtle" textAlign="end">
        {keys.length
          ? translation("settingMapEntries", { count: keys.length, names: keys.join(", ") })
          : translation("settingMapEmpty")}
      </Text>
    );
  }

  return (
    <Flex align="center" gap={1.5} w="100%">
      <TextField {...props} />
      {reset}
    </Flex>
  );
}
