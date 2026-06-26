import type { IconType } from "react-icons";
import { LuBrain, LuCrosshair, LuHourglass, LuTarget } from "react-icons/lu";

export interface FocusIcon {
  icon: IconType;
  color: string;
}

// Maps a focus indicator's icon hint to its glyph + accent color. Shared by the
// main chat thinking indicator and the sub-agent step cards so both render the
// same family of focus states: focus (explicit focus-set call), goal (target),
// waiting (hourglass), or plain reasoning (brain).
export function getFocusIcon(icon: string | undefined, title?: string): FocusIcon {
  const isWaiting = icon === "waiting" || title === "Waiting for tools...";
  if (isWaiting) return { icon: LuHourglass, color: "blue.fg" };
  if (icon === "goal") return { icon: LuTarget, color: "teal.fg" };
  if (icon === "focus") return { icon: LuCrosshair, color: "orange.fg" };
  return { icon: LuBrain, color: "purple.fg" };
}
