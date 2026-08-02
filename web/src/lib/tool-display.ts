import type { IconType } from "react-icons";

import { toolCallDisplay, type Translate } from "@shared/tools";

import { glyph } from "./glyphs";

/**
 * What a tool call is called, and which glyph stands for it — for this client.
 *
 * The deciding moved to `@shared/tools`, because it had been made twice: once here and once on
 * the phone, and the second copy was already drifting. What is left here is the one thing that
 * cannot be shared, which is turning a glyph *name* into a `react-icons` component.
 *
 * The translator is still passed in, so this client keeps `next-intl` — its locale, its plural
 * rules, its Japanese. The shared module falls back to the same catalogue's English only for a
 * caller that has no i18n framework at all.
 */

export type ToolDisplayTranslator = Translate;

interface ToolDisplayInfo {
  icon: IconType;
  iconColor: string;
  label: string;
  known: boolean;
  mono: boolean;
  labelIsMarkdown: boolean;
}

export function getToolCallDisplay(
  name: string,
  args: Record<string, unknown> | undefined,
  translation: ToolDisplayTranslator,
): ToolDisplayInfo {
  const display = toolCallDisplay(name, args, translation);
  return {
    icon: glyph(display.glyph),
    iconColor: display.tint,
    label: display.label,
    known: display.known,
    mono: display.mono,
    labelIsMarkdown: display.labelIsMarkdown,
  };
}
