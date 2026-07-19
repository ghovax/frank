"use client";

// The inline label shown in a tool-call heading. Three cases, decided by
// getToolCallDisplay:
//   • an unrecognized tool with no justification → its raw name in monospace
//     (it is an identifier, so it reads as code);
//   • a tool with a justification → the justification rendered as inline Markdown
//     (code spans, `file:line` refs, emphasis all render);
//   • otherwise → the plain fallback label (a raw command/path), kept literal so
//     Markdown never mangles it.
// Returns inline nodes only — the caller owns sizing, truncation, and shimmer.

import { Code } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import { InlineMarkdown } from "./markdown-content";

export function ToolCallLabel({ name, args }: { name: string; args?: Record<string, unknown> }) {
  const t = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const { label, mono, labelIsMarkdown } = getToolCallDisplay(name, args, t);
  if (mono) {
    return (
      <Code fontSize="0.9em" px={1} py={0} borderRadius="sm" fontFamily="var(--app-font-mono)" whiteSpace="nowrap">
        {label}
      </Code>
    );
  }
  if (labelIsMarkdown) return <InlineMarkdown content={label} />;
  return <>{label}</>;
}
