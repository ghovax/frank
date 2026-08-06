import type { IconType } from "react-icons";

import { CONCEPT_GLYPHS, CONCEPT_TINTS } from "@shared/tools";

import { glyph } from "./glyphs";

// One icon per concept, for the whole interface.
//
// These were chosen twice, independently: the capability browser on the blank-conversation page
// picked its own, and `tool-display.ts` picked its own for the transcript. The two sets
// collided, and the collisions were the misleading kind — the same glyph standing for different
// things, and the same thing wearing different glyphs depending on where you met it:
//
//   • `LuListChecks` was the "Skills available" heading *and* the `set_tasks` tool. A skill is
//     not a task list.
//   • `LuPuzzle` was a skill row *and* every MCP tool call. A skill is not an MCP tool.
//   • `LuWrench` was the "Tools available" heading *and* the fallback for a tool nobody
//     recognises — so the heading over the tools looked like an error state.
//   • A skill was `LuPuzzle` in the browser but `LuSparkles` when `load_skill` ran; an MCP
//     server was `LuPlug` in the browser but `LuPuzzle` when one of its tools was called. The
//     same thing, two faces, depending on the screen.
//
// So the vocabulary lives here and both surfaces read from it. The rule is one concept, one
// icon, and no icon meaning two things: `LuListChecks` is now only ever a task list, and
// `LuWrench` is now only ever "this tool is not one I know", which is what makes the fallback
// legible as a fallback.
//
// The choices themselves now live in `@shared/tools`, because the phone makes the same ones and
// a second table is how the two stop agreeing. What stays here is turning each name into a
// `react-icons` component.
export const CONCEPT_ICONS = {
  skill: glyph(CONCEPT_GLYPHS.skill),
  mcp: glyph(CONCEPT_GLYPHS.mcp),
  tasks: glyph(CONCEPT_GLYPHS.tasks),
  environment: glyph(CONCEPT_GLYPHS.environment),
  unknownTool: glyph(CONCEPT_GLYPHS.unknownTool),
} satisfies Record<string, IconType>;

export const CONCEPT_ICON_COLORS = CONCEPT_TINTS;
