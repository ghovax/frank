"use client";

import { useEffect, useState } from "react";

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
