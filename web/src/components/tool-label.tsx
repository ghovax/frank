"use client";

// The inline label in a tool-call heading: the model's own explanation, rendered as the Markdown it is.

import { getToolCallDisplay } from "@/lib/glyphs";
import { InlineMarkdown } from "./markdown-content";

export function ToolCallLabel({ name, args }: { name: string; args?: Record<string, unknown> }) {
  const { label } = getToolCallDisplay(name, args);
  // Nothing to say yet, so nothing is said: the icon and the row already show that something is running.
  if (!label) return null;
  return <InlineMarkdown content={label} />;
}
