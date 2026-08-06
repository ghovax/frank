import type { IconType } from "react-icons";

import { CONCEPT_GLYPHS, CONCEPT_TINTS } from "@shared/tools";

import { glyph } from "./glyphs";

// One icon per concept for the whole interface, so two surfaces cannot pick different ones.
export const CONCEPT_ICONS = {
  skill: glyph(CONCEPT_GLYPHS.skill),
  mcp: glyph(CONCEPT_GLYPHS.mcp),
  tasks: glyph(CONCEPT_GLYPHS.tasks),
  environment: glyph(CONCEPT_GLYPHS.environment),
  unknownTool: glyph(CONCEPT_GLYPHS.unknownTool),
} satisfies Record<string, IconType>;

export const CONCEPT_ICON_COLORS = CONCEPT_TINTS;
