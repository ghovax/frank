"use client";

// Every instant the reader sees, worded as distance from now, since "2 hours ago" is what they actually wanted to know.

import { Span, type SpanProps } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { useFormatter } from "next-intl";

// One timer for the window rather than one per caller, since a transcript holds hundreds of these.
const listeners = new Set<(now: Date) => void>();
let ticker: number | null = null;

function subscribe(listener: (now: Date) => void): () => void {
  listeners.add(listener);
  if (ticker === null) {
    ticker = window.setInterval(() => {
      const now = new Date();
      for (const each of listeners) each(now);
    }, 60_000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && ticker !== null) {
      window.clearInterval(ticker);
      ticker = null;
    }
  };
}

/** A client clock that keeps ticking, since the provider's is frozen at mount and drifts into the past. */
export function useClock(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => subscribe(setNow), []);
  return now;
}

// Spelled out in full, since a tooltip is opened by somebody who wants the exact instant.
const EXACT = {
  weekday: "long",
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
} as const;

function instant(value: string | number | Date): Date | null {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** How long ago, or how long until, re-worded as it ages; for the callers that need a plain string. */
export function useRelative(): (value: string | number | Date) => string {
  const format = useFormatter();
  const now = useClock();
  return (value) => {
    const date = instant(value);
    return date ? format.relativeTime(date, now) : "";
  };
}

/** The same, with the exact instant on the title, which is where precision belongs. */
export function RelativeTime({ date, ...rest }: { date: string | number | Date } & SpanProps) {
  const format = useFormatter();
  const relative = useRelative();
  const at = instant(date);
  if (!at) return null;
  return (
    <Span title={format.dateTime(at, EXACT)} {...rest}>
      {relative(at)}
    </Span>
  );
}
