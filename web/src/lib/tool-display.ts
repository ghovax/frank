import type { IconType } from "react-icons";

import { toolCallDisplay, type Translate } from "@shared/tools";

import { glyph } from "./glyphs";

/** What a tool call is called and which glyph stands for it, for this client, over the shared deciding. */

export type ToolDisplayTranslator = Translate;

interface ToolDisplayInfo {
  icon: IconType;
  iconColor: string;
  label: string;
  known: boolean;
  mono: boolean;
  labelIsMarkdown: boolean;
}

export function getToolCallDisplay(
  name: string,
  args: Record<string, unknown> | undefined,
  translation: ToolDisplayTranslator,
): ToolDisplayInfo {
  const display = toolCallDisplay(name, args, translation);
  return {
    icon: glyph(display.glyph),
    iconColor: display.tint,
    label: display.label,
    known: display.known,
    mono: display.mono,
    labelIsMarkdown: display.labelIsMarkdown,
  };
}

// Whether a call declared it changes nothing, and what reach it asked for beyond its sandbox.
export function declaredNonMutating(args: Record<string, unknown> | undefined): boolean {
  const request = args?.access_request;
  if (!request || typeof request !== "object") return false;
  return (request as Record<string, unknown>).mutates === false;
}

export interface RequestedAccess {
  reads: string[];
  writes: string[];
  network: boolean;
  /** Whether the call asked for anything at all, since a bare `mutates` is a claim rather than a request. */
  any: boolean;
}

// Always a value and never null, so a caller reads its fields without a guard on every line.
export function requestedAccess(args: Record<string, unknown> | undefined): RequestedAccess {
  const request = args?.access_request;
  const empty: RequestedAccess = { reads: [], writes: [], network: false, any: false };
  if (!request || typeof request !== "object") return empty;
  const record = request as Record<string, unknown>;
  const paths = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
  const reads = paths(record.reads);
  const writes = paths(record.writes);
  const network = record.network === true;
  return { reads, writes, network, any: reads.length > 0 || writes.length > 0 || network };
}
