"use client";

import { Span } from "@chakra-ui/react";
import type { ReactNode } from "react";

// A number centered in its container by CSS constraints alone: an absolutely-positioned flex layer fills the parent (which must be `position: relative`, or already positioned) and centers the glyph with `align-items`/`justify-content`.
export function CenteredNumber({
  children,
  fontSize = 12,
  weight = 600,
}: {
  children: ReactNode;
  fontSize?: number;
  weight?: number;
}) {
  return (
    <Span
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize,
        fontWeight: weight,
        fontVariantNumeric: "tabular-nums",
        textBox: "trim-both cap alphabetic",
        pointerEvents: "none",
      }}
      aria-hidden
    >
      {children}
    </Span>
  );
}
