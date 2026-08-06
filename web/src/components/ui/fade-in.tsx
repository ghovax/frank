"use client";

import { motion } from "motion/react";
import type { CSSProperties, ReactNode } from "react";

// The only entrance the transcript is allowed to use, and it exists to make the wrong thing unavailable rather than merely discouraged.

export interface FadeInProps {
  children: ReactNode;
  // Skips the animation entirely — for rows restored from history or loaded with a session, which were never "new" and should simply be present.
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
