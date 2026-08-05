"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  LuActivity, LuFolderGit2, LuGlobe, LuKeyRound, LuLayers, LuMic, LuMonitor,
  LuPackage, LuPlug, LuScale, LuSearch, LuServer, LuShield, LuSlidersHorizontal, LuUser, LuUsers,
} from "react-icons/lu";
import { toaster } from "@/components/ui/toaster";
import { DisclosureLabel, DisclosureRow } from "@/components/ui/disclosure-row";
import { swallowed } from "@/lib/swallowed";
import {
  fetchSettingsSchema,
  resetSettingValue,
  subscribeEvents,
  updateSettingValue,
  type SettingEntry,
  type SettingsSectionSchema,
} from "@/lib/api";
import type { SettingRowDef, SettingsPage } from "@/lib/settings-model";
import { SettingControl } from "./setting-control";

// Everything the configuration file holds, as one page of the panel.
//
// One page, not nineteen. A section per entry in the left rail put the whole file in the
// navigation and made the five pages a person actually visits hard to find among the settings
// they open once a year — the rail is for the handful of places you go, and this is one place
// with a lot in it. So the sections nest: an icon, a name, a chevron, and rows underneath, which
// is the same disclosure the transcript's tool calls use.
//
// Nothing here names a setting. What a setting *is* — its type, its choices, whether it is a
// credential, what it is set to — comes from the daemon, which walks the schema. What it is
// *called*, the sentence under it, and the words for the values it may take come from the
// message catalogue, keyed by the same dotted path, because those are words to translate and a
// schema is not. That split is why switching language changes them: there is no English on the
// wire to be stuck with.

/** One icon per section, so a collapsed list reads as places rather than as nineteen lines of
 * text. Chosen for what the section *is about* — a shield for confinement, a scale for the
 * judge, sliders for the numbers underneath everything. */
const SECTION_ICONS: Record<string, React.ComponentType<{ size?: number }>> = {
  agent: LuUser,
  workspace: LuFolderGit2,
  sandbox: LuShield,
  toolbox: LuPackage,
  permission_classifier: LuScale,
  compaction: LuLayers,
  user_context: LuUser,
  computer_control: LuMonitor,
  dictation: LuMic,
  providers: LuKeyRound,
  exa: LuSearch,
  jina: LuGlobe,
  firecrawl: LuGlobe,
  web_fetch: LuGlobe,
  composio: LuPlug,
  mcp: LuServer,
  remote_agents: LuUsers,
  telemetry: LuActivity,
  tuning: LuSlidersHorizontal,
};

/** A translator that takes a path resolved at runtime.
 *
 * `next-intl` types its keys against the catalogue, which is right for the hand-written strings
 * and wrong here: the key *is* the setting's dotted path, known only when the daemon answers. */
type PathTranslator = {
  (path: string): string;
  has: (path: string) => boolean;
  raw: (path: string) => unknown;
};

/** The string at a path, whether the catalogue holds it as a leaf or as a group.
 *
 * A group is an object — its own name under `_`, its children beside it — and one path can be
 * both: `providers` is the section *and* the open-ended map inside it, so `Settings.providers`
 * has to be an object and its name has to be reachable from the same path a leaf would use.
 * Reading the raw node and looking for `_` when it is not a string is that rule, stated once;
 * without it the panel rendered the literal key `Settings.providers._`. */
export function textAt(translator: PathTranslator, path: string): string {
  const raw = translator.raw(path);
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object") {
    const own = (raw as Record<string, unknown>)._;
    if (typeof own === "string") return own;
  }
  return "";
}

/** The group a setting belongs to: everything but its last segment, or the section itself. */
function groupOf(path: string): string {
  const parts = path.split(".");
  return parts.length > 2 ? parts.slice(0, -1).join(".") : parts[0];
}

/** One section of the configuration file, as a collapsible block of rows. */
function ConfigurationSection({
  section,
  rowsByGroup,
  renderRow,
}: {
  section: SettingsSectionSchema;
  rowsByGroup: Map<string, SettingRowDef[]>;
  renderRow: (row: SettingRowDef) => React.ReactNode;
}) {
  const names = useTranslations("Settings") as unknown as PathTranslator;
  const about = useTranslations("SettingsAbout") as unknown as PathTranslator;
  const Icon = SECTION_ICONS[section.path] ?? LuSlidersHorizontal;
  const sentence = textAt(about, section.path);
  return (
    <DisclosureRow icon={<Icon size={13} />} title={<DisclosureLabel>{textAt(names, section.path)}</DisclosureLabel>}>
      <Flex direction="column" gap={4} pt={1} pb={2}>
        {sentence ? <Text fontSize="xs" color="fg.muted">{sentence}</Text> : null}
        {[...rowsByGroup.entries()].map(([group, rows]) => (
          <Box key={group}>
            {/* A sub-object of the section — `sandbox.filesystem`, `telemetry.exporter` — gets
                its own heading only where the section has more than one, so a section with a
                single group is not a heading over a heading. */}
            {rowsByGroup.size > 1 ? (
              <Text textStyle="sectionLabel" mb={1}>{textAt(names, group)}</Text>
            ) : null}
            <Box css={{ "& > *": { borderColor: "var(--chakra-colors-border-muted)" }, "& > *:not(:last-child)": { borderBottomWidth: "1px" } }}>
              {rows.map(renderRow)}
            </Box>
          </Box>
        ))}
      </Flex>
    </DisclosureRow>
  );
}

export function useSchemaSettingsPage(renderRow: (row: SettingRowDef) => React.ReactNode): SettingsPage | null {
  const translation = useTranslations("SettingsDialog");
  const names = useTranslations("Settings") as unknown as PathTranslator;
  const about = useTranslations("SettingsAbout") as unknown as PathTranslator;
  const [sections, setSections] = useState<SettingsSectionSchema[]>([]);
  // The paths a write is in flight for. A control stays live while another is saving, so
  // changing one setting never freezes the page.
  const [saving, setSaving] = useState<Set<string>>(new Set());

  const load = useCallback(() => {
    fetchSettingsSchema()
      .then(setSections)
      .catch((caught) => swallowed({ component: "settings-schema", operation: "read the settings schema" }, caught));
  }, []);

  useEffect(() => {
    load();
    // The same event every other settings surface listens to, so a change made in the terminal,
    // in another window, or by this panel lands here without a reload.
    const unsubscribe = subscribeEvents((event) => { if (event.type === "settings_changed") load(); });
    return unsubscribe;
  }, [load]);

  const apply = useCallback(async (path: string, run: () => Promise<void>) => {
    setSaving((current) => new Set(current).add(path));
    try {
      await run();
    } catch (caught) {
      // The daemon's own words. It refused because the schema refused, and "Input should be
      // 'required', 'preferred' or 'off'" is the sentence worth reading — a generic "could not
      // save" would replace the only useful thing with nothing.
      toaster.create({
        type: "error",
        title: translation("settingsSchemaError", { reason: caught instanceof Error ? caught.message : String(caught) }),
        closable: true,
      });
    } finally {
      setSaving((current) => {
        const next = new Set(current);
        next.delete(path);
        return next;
      });
      load();
    }
  }, [load, translation]);

  return useMemo<SettingsPage | null>(() => {
    if (sections.length === 0) return null;
    const rowFor = (entry: SettingEntry): SettingRowDef => ({
      key: entry.path,
      title: textAt(names, entry.path),
      description: textAt(about, entry.path) || undefined,
      // A list is as tall as its entries and a path as wide as the path: both drop the control
      // to its own line, the treatment an API-key field already gets.
      layout: entry.kind === "list" || entry.kind === "string" ? "stacked" : "row",
      control: (
        <SettingControl
          entry={entry}
          busy={saving.has(entry.path)}
          onChange={(value) => apply(entry.path, () => updateSettingValue(entry.path, value))}
          onReset={() => apply(entry.path, () => resetSettingValue(entry.path))}
        />
      ),
    });
    return {
      id: "configuration",
      label: translation("tabConfiguration"),
      icon: <LuSlidersHorizontal size={14} />,
      sections: [{
        title: translation("tabConfiguration"),
        rows: [],
        block: (
          <Flex direction="column" gap={1} w="100%">
            {sections.map((section) => {
              const rowsByGroup = new Map<string, SettingRowDef[]>();
              for (const entry of section.settings) {
                const group = groupOf(entry.path);
                const existing = rowsByGroup.get(group);
                if (existing) existing.push(rowFor(entry)); else rowsByGroup.set(group, [rowFor(entry)]);
              }
              return (
                <ConfigurationSection
                  key={section.path}
                  section={section}
                  rowsByGroup={rowsByGroup}
                  renderRow={renderRow}
                />
              );
            })}
          </Flex>
        ),
      }],
    };
  }, [sections, saving, apply, names, about, translation, renderRow]);
}
