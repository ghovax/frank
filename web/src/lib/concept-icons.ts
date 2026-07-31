import type { IconType } from "react-icons";
import { LuListChecks, LuPlug, LuServer, LuSparkles, LuWrench } from "react-icons/lu";

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
export const CONCEPT_ICONS = {
  /** A skill: something the agent knows how to do. Also `load_skill`, which loads one. */
  skill: LuSparkles,
  /** MCP — a configured server, its tools, and every call into one. */
  mcp: LuPlug,
  /** The agent's own task list (`set_tasks` / `update_tasks`), and nothing else. */
  tasks: LuListChecks,
  /** A place this workspace can work in — a folder here, or one on an SSH host. */
  environment: LuServer,
  /** A tool the interface does not recognise. Only ever the fallback. */
  unknownTool: LuWrench,
} satisfies Record<string, IconType>;

// The colour each concept carries where colour is used (the transcript's activity lines). Kept
// beside the glyphs so a concept cannot pick up a new icon and keep an old colour.
export const CONCEPT_ICON_COLORS = {
  skill: "pink.fg",
  mcp: "purple.fg",
  tasks: "blue.fg",
  environment: "fg.muted",
  unknownTool: "fg.muted",
} satisfies Record<keyof typeof CONCEPT_ICONS, string>;
