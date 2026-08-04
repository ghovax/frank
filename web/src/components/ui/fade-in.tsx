"use client";

import { motion } from "motion/react";
import type { CSSProperties, ReactNode } from "react";

// The only entrance the transcript is allowed to use, and it exists to make the wrong thing
// unavailable rather than merely discouraged.
//
// **A row may fade in. It may never fade out.** Fading in is free: the element is already
// mounted at its full height, so nothing below it moves and only its ink appears. Fading out is
// the opposite — the element keeps its height for the whole animation and then collapses, so
// everything beneath it sits in the wrong place for the duration and then jumps. That is the
// vertical shift, and it cannot be tuned away by shortening the duration; it can only be not
// done. This component therefore takes no `exit` and forwards none, so a caller cannot ask for
// one without deleting the component and writing `motion.div` by hand — which is a visible act
// in review rather than one more prop on an existing element.
//
// The two exceptions elsewhere in the app are both safe for structural reasons, and neither
// should use this: `AnimatePresence mode="popLayout"` takes an exiting child out of flow before
// animating it (so nothing below it can move), and an `inline-flex` badge inside a row only ever
// changes that row's width.

export interface FadeInProps {
  children: ReactNode;
  // Skips the animation entirely — for rows restored from history or loaded with a session,
  // which were never "new" and should simply be present.
  animate?: boolean;
  seconds?: number;
  style?: CSSProperties;
}

export function FadeIn({ children, animate = true, seconds = 0.18, style }: FadeInProps) {
  return (
    <motion.div
      initial={animate ? { opacity: 0 } : false}
      animate={{ opacity: 1 }}
      transition={{ duration: seconds, ease: "easeOut" }}
      style={style}
    >
      {children}
    </motion.div>
  );
}
