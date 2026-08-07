"use client";

// The inline label in a tool-call heading: a raw name, an explanation, or a translated one.

import { Code, Skeleton } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import { InlineMarkdown } from "./markdown-content";
import { FadeIn } from "@/components/ui/fade-in";

export function ToolCallLabel({ name, args, settled = true }: { name: string; args?: Record<string, unknown>; settled?: boolean }) {
  const translation = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const { label, mono, labelIsMarkdown } = getToolCallDisplay(name, args, translation, settled);
  // Nothing to say yet: a bar holds the line's height so the row does not jump when the words arrive.
  if (!label) return <Skeleton height="0.9em" width="14ch" borderRadius="sm" opacity={0.5} />;
  const content = mono ? (
    <Code fontSize="0.9em" px={1} py={0} borderRadius="sm" fontFamily="var(--app-font-mono)" whiteSpace="nowrap">
      {label}
    </Code>
  ) : labelIsMarkdown ? (
    <InlineMarkdown content={label} />
  ) : (
    label
  );
  // The entrance the streamed prose uses, since this arrives the same way and should read the same.
  return <FadeIn inline>{content}</FadeIn>;
}
