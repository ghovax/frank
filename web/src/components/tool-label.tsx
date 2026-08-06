"use client";

// The inline label shown in a tool-call heading.

import { Code } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import { InlineMarkdown } from "./markdown-content";

export function ToolCallLabel({ name, args }: { name: string; args?: Record<string, unknown> }) {
  const translation = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const { label, mono, labelIsMarkdown } = getToolCallDisplay(name, args, translation);
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
