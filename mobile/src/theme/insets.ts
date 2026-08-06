import { useSafeAreaInsets } from "react-native-safe-area-context";

import { space } from "./tokens";

/** The safe-area insets, floored so nothing ever sits against an edge. */
export function useEdgeInsets() {
  const insets = useSafeAreaInsets();
  return {
    top: Math.max(insets.top, space[3]),
    bottom: Math.max(insets.bottom, space[3]),
    left: Math.max(insets.left, 0),
    right: Math.max(insets.right, 0),
  };
}
