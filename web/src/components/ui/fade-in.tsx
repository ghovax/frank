"use client";

import { motion } from "motion/react";
import type { CSSProperties, ReactNode } from "react";

// The only entrance the transcript may use: a row may fade in, and may never fade out.

export interface FadeInProps {
  children: ReactNode;
  // Skips the animation for rows restored from history, which were never new.
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
