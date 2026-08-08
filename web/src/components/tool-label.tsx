"use client";

// The inline label in a tool-call heading: the model's own explanation, rendered as the Markdown it is.

import { getToolCallDisplay } from "@/lib/glyphs";
import { InlineMarkdown } from "./markdown-content";

export function ToolCallLabel({
  name,
  args,
  ready = false,
}: {
  name: string;
  args?: Record<string, unknown>;
  ready?: boolean;
}) {
  const { label } = getToolCallDisplay(name, args, ready);
  if (!label) return null;
  return <InlineMarkdown content={label} />;
}
