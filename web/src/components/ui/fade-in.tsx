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
  // For arriving text inside a line, where a block would break the line it belongs to.
  inline?: boolean;
}

export function FadeIn({ children, animate = true, seconds = 0.18, style, inline = false }: FadeInProps) {
  const Element = inline ? motion.span : motion.div;
  return (
    <Element
      initial={animate ? { opacity: 0 } : false}
      animate={{ opacity: 1 }}
      transition={{ duration: seconds, ease: "easeOut" }}
      style={style}
    >
      {children}
    </Element>
  );
}
