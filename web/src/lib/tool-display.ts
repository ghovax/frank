import type { IconType } from "react-icons";

import { toolCallDisplay, type Translate } from "@shared/tools";

import { glyph } from "./glyphs";

/**
 * What a tool call is called, and which glyph stands for it — for this client.
 *
 * The deciding moved to `@shared/tools`, because it had been made twice: once here and once on
 * the phone, and the second copy was already drifting. What is left here is the one thing that
 * cannot be shared, which is turning a glyph *name* into a `react-icons` component.
 *
 * The translator is still passed in, so this client keeps `next-intl` — its locale, its plural
 * rules, its Japanese. The shared module falls back to the same catalogue's English only for a
 * caller that has no i18n framework at all.
 */

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

// Whether a tool call declared that it changes nothing, and what reach it asked for beyond its
// sandbox. Both read off `access_request`, which replaced the separate `read_only` flag.
//
// One reading, in one place, because the two call sites that needed it had drifted apart from
// the harness and from each other: both treated an *absent* declaration as read-only, while the
// harness treats it as mutating. So a command that took a filesystem lease and ran as a write
// showed no write badge, and the marker meant to be a safety signal was quietly absent on
// exactly the calls a person would want it for.
export function declaredNonMutating(args: Record<string, unknown> | undefined): boolean {
  const request = args?.access_request;
  if (!request || typeof request !== "object") return false;
  return (request as Record<string, unknown>).mutates === false;
}

export interface RequestedAccess {
  reads: string[];
  writes: string[];
  network: boolean;
  /**
   * Whether the call asked for anything at all. A request that only states `mutates` is a claim
   * about the call, not a request for access, so it must not render as one — otherwise every
   * read-only command grows an empty access row.
   */
  any: boolean;
}

// Always a value, never null, so a caller reads `.writes` without a guard on every line. The
// nullable version pushed a `access &&` in front of each field, and the one place that forgot it
// would have thrown while rendering a tool call.
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
