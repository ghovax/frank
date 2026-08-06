/**
 * What every runtime setting offers, and how each choice looks.
 *
 * The desktop has exactly two control shapes and no others: a **toggle** — a chip whose label,
 * glyph and colour all change with its state — and a **select** — a chip that opens a list of
 * options, each with a label and a sentence saying what it does. Every setting is one of the two.
 * There is not a switch anywhere in it.
 *
 * The phone had switches, which is why it looked like a different application: a switch says
 * "on/off" and nothing about what on *means*, so the state had to be re-explained beside it in
 * words the desktop does not use. Both clients now build their controls from the definitions
 * here — same options, same order, same labels, same colours — and differ only in how a list is
 * presented, which is a popover on a desktop and a sheet on a phone because that is what those
 * platforms have.
 *
 * Colours are palette names and glyphs are glyph names, resolved per client exactly as in
 * `tools.ts`.
 */

import { labels, type Namespace } from "./labels";
import type { GlyphName } from "./tools";

export interface ChoiceDefinition<Value extends string = string> {
  value: Value;
  /** The catalogue key for the short label the chip shows — an action, as a control reads. */
  labelKey: string;
  /** The catalogue key for the thing's *name*, as a field reads it. Defaults to `labelKey`. */
  nameKey?: string;
  /** The catalogue key for the sentence saying what choosing it does, where there is one. */
  descriptionKey?: string;
  glyph: GlyphName;
  /** A palette name, or omitted for the neutral one. */
  palette?: string;
}

export interface ChoiceSet<Value extends string = string> {
  /** Which namespace the keys above live in. */
  namespace: Namespace | string;
  /** The catalogue key for what the setting itself is called. */
  titleKey: string;
  titleNamespace: Namespace | string;
  /** The catalogue key for the setting's own explanation, where it has one. */
  hintKey?: string;
  choices: ChoiceDefinition<Value>[];
}

export type PermissionModeValue = "ask" | "auto";
export type SandboxEnforceValue = "required" | "off";
export type WorktreeStrategyValue = "none" | "branch" | "worktree";
export type BooleanValue = "on" | "off";

/**
 * Who answers when a call asks to reach past the box the session runs in. Everything inside that
 * box runs without asking anybody, so this is not a spectrum of how much the agent may do — it is
 * one question: when it wants out, are you the one who says yes.
 */
export const PERMISSION_MODES: ChoiceSet<PermissionModeValue> = {
  namespace: "SessionControls",
  titleNamespace: "SettingsDialog",
  titleKey: "approvalMode",
  choices: [
    {
      value: "ask",
      labelKey: "permissionAskLabel",
      nameKey: "permissionAskName",
      descriptionKey: "permissionAskDescription",
      glyph: "hand",
    },
    {
      value: "auto",
      labelKey: "permissionAutoLabel",
      nameKey: "permissionAutoName",
      descriptionKey: "permissionAutoDescription",
      glyph: "badge-check",
      palette: "blue",
    },
  ],
};

/**
 * Whether a session's tool children are confined to the filesystem it was given.
 *
 * `off` is red rather than neutral, deliberately: it is the one setting here whose "off" removes
 * a boundary rather than a convenience.
 */
export const SANDBOX_ENFORCE: ChoiceSet<SandboxEnforceValue> = {
  namespace: "SessionControls",
  titleNamespace: "SettingsDialog",
  titleKey: "filesystemProtection",
  choices: [
    { value: "required", labelKey: "sandboxRestricted", glyph: "box", palette: "green" },
    { value: "off", labelKey: "sandboxGlobal", glyph: "globe", palette: "red" },
  ],
};

/** What a machine with no confinement backend shows instead — a state, not a choice. */
export const SANDBOX_UNAVAILABLE = {
  labelKey: "sandboxUnavailable",
  glyph: "globe" as GlyphName,
  palette: "orange",
  hintKey: "filesystemProtectionUnavailable",
};

export const WORKTREE_STRATEGIES: ChoiceSet<WorktreeStrategyValue> = {
  namespace: "SessionControls",
  titleNamespace: "SettingsDialog",
  titleKey: "gitWorktree",
  choices: [
    { value: "none", labelKey: "worktreeNoneLabel", descriptionKey: "worktreeNoneDescription", glyph: "folder" },
    { value: "branch", labelKey: "worktreeBranchLabel", descriptionKey: "worktreeBranchDescription", glyph: "git-branch", palette: "blue" },
    { value: "worktree", labelKey: "worktreeCopyLabel", descriptionKey: "worktreeCopyDescription", glyph: "copy", palette: "blue" },
  ],
};

export const COMPACTION: ChoiceSet<BooleanValue> = {
  namespace: "SessionControls",
  titleNamespace: "SettingsDialog",
  titleKey: "compaction",
  choices: [
    { value: "on", labelKey: "compactionAutomatic", glyph: "zap", palette: "blue" },
    { value: "off", labelKey: "compactionManual", glyph: "circle-slash" },
  ],
};

export const USER_CONTEXT: ChoiceSet<BooleanValue> = {
  namespace: "SessionControls",
  titleNamespace: "SettingsDialog",
  titleKey: "userContext",
  hintKey: "userContextHint",
  choices: [
    { value: "on", labelKey: "userContextOn", glyph: "user-search", palette: "blue" },
    { value: "off", labelKey: "userContextOff", glyph: "user-round-x" },
  ],
};

export const COMPUTER_CONTROL: ChoiceSet<BooleanValue> = {
  namespace: "SessionControls",
  titleNamespace: "SettingsDialog",
  titleKey: "computerControl",
  hintKey: "computerControlHint",
  choices: [
    { value: "on", labelKey: "computerControlOn", glyph: "mouse-pointer-click", palette: "blue" },
    { value: "off", labelKey: "computerControlOff", glyph: "circle-slash" },
  ],
};

export const DICTATION: ChoiceSet<BooleanValue> = {
  namespace: "SessionControls",
  titleNamespace: "SettingsDialog",
  titleKey: "dictation",
  hintKey: "dictationHint",
  choices: [
    { value: "on", labelKey: "dictationOn", glyph: "mic", palette: "blue" },
    { value: "off", labelKey: "dictationOff", glyph: "mic-off" },
  ],
};

/** One choice, resolved: the words instead of the keys. */
export interface Choice<Value extends string = string> {
  value: Value;
  label: string;
  description: string;
  glyph: GlyphName;
  palette?: string;
}

export function resolveChoices<Value extends string>(set: ChoiceSet<Value>): Choice<Value>[] {
  const say = labels(set.namespace);
  return set.choices.map((choice) => ({
    value: choice.value,
    label: say(choice.labelKey),
    description: choice.descriptionKey ? say(choice.descriptionKey) : "",
    glyph: choice.glyph,
    palette: choice.palette,
  }));
}

/** What a setting is called, and the sentence under it. */
export function resolveHeading(set: ChoiceSet<string>): { title: string; hint: string } {
  const say = labels(set.titleNamespace);
  return { title: say(set.titleKey), hint: set.hintKey ? say(set.hintKey) : "" };
}
