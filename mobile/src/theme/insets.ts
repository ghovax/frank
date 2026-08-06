import { useSafeAreaInsets } from "react-native-safe-area-context";

import { space } from "./tokens";

/**
 * The safe-area insets, floored so nothing ever sits against an edge.
 *
 * `useSafeAreaInsets` answers what the *hardware* takes — a notch, a home indicator, a rounded
 * corner — and on a device with none of those it answers zero. Zero is true and useless: a
 * header flush against the top of the glass reads as broken whether or not anything is
 * physically in the way, and on a phone with rounded corners the first and last rows are exactly
 * where the curve eats them.
 *
 * So the inset is the larger of what the hardware needs and what the layout needs. The desktop
 * has the same idea in one line — `pt="calc(var(--titlebar-height, 0px) + 8px)"`.
 */
export function useEdgeInsets() {
  const insets = useSafeAreaInsets();
  return {
    top: Math.max(insets.top, space[3]),
    bottom: Math.max(insets.bottom, space[3]),
    left: Math.max(insets.left, 0),
    right: Math.max(insets.right, 0),
  };
}
