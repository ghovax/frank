"use client";

import { Span } from "@chakra-ui/react";
import type { ReactNode } from "react";

// A number centred by CSS constraints alone, since flex alone centres the glyph's box and not its ink.
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
