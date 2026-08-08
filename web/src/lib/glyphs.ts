import type { IconType } from "react-icons";

import { toolCallDisplay, type GlyphName } from "@shared/tools";
import {
  LuBadgeCheck,
  LuBox,
  LuCircleAlert,
  LuCircleSlash,
  LuClock,
  LuCircleX,
  LuCopy,
  LuDownload,
  LuFolder,
  LuGitBranch,
  LuGlobe,
  LuHand,
  LuHistory,
  LuListChecks,
  LuMessageCircleQuestion,
  LuMic,
  LuMicOff,
  LuMoon,
  LuMousePointerClick,
  LuPlug,
  LuRadioTower,
  LuServer,
  LuSparkles,
  LuTarget,
  LuTerminal,
  LuUserRoundX,
  LuUsers,
  LuUserSearch,
  LuWrench,
  LuZap,
  LuUserPlus,
  LuSend,
  LuList,
  LuPlugZap,
  LuBoxes,
  LuBookOpen,
  LuSatelliteDish,
  LuSquareCheck,
  LuHardDriveDownload,
} from "react-icons/lu";

/** The one table turning a shared glyph name into something this client can draw. */
export const GLYPHS: Record<GlyphName, IconType> = {
  globe: LuGlobe,
  terminal: LuTerminal,
  "mouse-pointer-click": LuMousePointerClick,
  download: LuDownload,
  "message-circle-question": LuMessageCircleQuestion,
  target: LuTarget,
  "user-search": LuUserSearch,
  sparkles: LuSparkles,
  plug: LuPlug,
  "list-checks": LuListChecks,
  server: LuServer,
  users: LuUsers,
  "radio-tower": LuRadioTower,
  clock: LuClock,
  "user-plus": LuUserPlus,
  send: LuSend,
  list: LuList,
  "plug-zap": LuPlugZap,
  boxes: LuBoxes,
  "book-open": LuBookOpen,
  "satellite-dish": LuSatelliteDish,
  "square-check": LuSquareCheck,
  "hard-drive-download": LuHardDriveDownload,
  history: LuHistory,
  wrench: LuWrench,
  "circle-alert": LuCircleAlert,
  "circle-x": LuCircleX,
  moon: LuMoon,
  hand: LuHand,
  "badge-check": LuBadgeCheck,
  box: LuBox,
  folder: LuFolder,
  "git-branch": LuGitBranch,
  copy: LuCopy,
  zap: LuZap,
  "circle-slash": LuCircleSlash,
  "user-round-x": LuUserRoundX,
  mic: LuMic,
  "mic-off": LuMicOff,
};

/** A glyph by name, falling back to the one that stands for a tool with no glyph of its own. */
export function glyph(name: GlyphName | undefined): IconType {
  return (name && GLYPHS[name]) || LuWrench;
}

// One icon per concept for the whole interface, so two surfaces cannot pick different ones.
export const CONCEPT_ICONS = {
  /** A skill: something the agent knows how to do. */
  skill: glyph("sparkles"),
  /** MCP — a configured server, its tools, and every call into one. */
  mcp: glyph("plug"),
  /** A place this workspace can work in — a folder here, or one on an SSH host. */
  environment: glyph("server"),
} satisfies Record<string, IconType>;

interface ToolDisplayInfo {
  icon: IconType;
  iconColor: string;
  label: string;
}

/** What a tool call is called and which icon stands for it, for this client, over the shared deciding. */
export function getToolCallDisplay(
  name: string,
  args: Record<string, unknown> | undefined,
  ready = false,
): ToolDisplayInfo {
  const display = toolCallDisplay(name, args, ready);
  return { icon: glyph(display.glyph), iconColor: display.tint, label: display.label };
}
