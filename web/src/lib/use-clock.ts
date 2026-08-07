"use client";

import { useEffect, useState } from "react";

// One timer for the whole window, not one per caller: a transcript holds hundreds of timestamps, and a
// hundred intervals waking a hundred components every minute is what makes an interface feel erratic.
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

/** A clock that starts on the client and keeps ticking, for anything measured against "now".
 *
 * The provider's `now` is frozen at mount so that static rendering does not warn, which makes it wrong
 * by however long the window has been open — a message sent now would read as being in the future.
 */
export function useClock(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => subscribe(setNow), []);
  return now;
}
