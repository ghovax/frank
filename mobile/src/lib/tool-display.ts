import type { LucideIcon } from "lucide-react-native";

import { toolCallDisplay } from "@shared/tools";

import type { Palette } from "../theme/tokens";
import { glyph, tintKey } from "./glyphs";

/**
 * What a tool call is called, and which glyph stands for it — for this client.
 *
 * This file used to *decide* those things, in parallel with the desktop, and the two had already
 * drifted: different wording for the same call and a different glyph for `search_code`. The
 * deciding is now `@shared/tools`, which both read; what is left here is turning a glyph name
 * into a `lucide-react-native` component and a tint token into a key in this client's palette.
 *
 * No translator is passed, so the shared module falls back to the catalogue's English — the same
 * `shared/messages/en.json` the desktop's `en` comes from. When this client learns about locales
 * it passes its own reader in and nothing else changes.
 */
export interface ToolDisplay {
  icon: LucideIcon;
  tint: keyof Palette;
  label: string;
  mono: boolean;
}

export function toolDisplay(name: string, args: Record<string, unknown> | undefined): ToolDisplay {
  const display = toolCallDisplay(name, args);
  return {
    icon: glyph(display.glyph),
    tint: tintKey(display.tint) as keyof Palette,
    label: display.label,
    mono: display.mono,
  };
}
