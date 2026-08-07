"use client";

// The inline label in a tool-call heading: a raw name, an explanation, or a translated one.

import { Code } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import { InlineMarkdown } from "./markdown-content";

export function ToolCallLabel({ name, args, settled = true }: { name: string; args?: Record<string, unknown>; settled?: boolean }) {
  const translation = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const { label, mono, labelIsMarkdown } = getToolCallDisplay(name, args, translation, settled);
  // Nothing to say yet, so nothing is said: the icon and the row already show that something is running.
  if (!label) return null;
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
