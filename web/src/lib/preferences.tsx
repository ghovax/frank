"use client";

// What the interface remembers about itself, held for the render.
//
// Nothing here persists anything. The daemon is the source of truth: this asks it once before
// the first paint, hands the answer down, and asks again whenever the daemon says it changed.
// A change goes the same way — post it, and use what comes back. That is why the colour mode
// you pick in the desktop app arrives in the browser window beside it, and why clearing a
// browser's storage no longer resets Frank to defaults.
//
// Children render only once the answer is in. The theme and the language are read from it, and
// rendering the tree before it lands would paint the default and correct it a frame later: a
// flash of white in a dark room, and a flash of English. The wait is one request to a daemon
// on this machine, and a failed one resolves to the defaults rather than to a blank screen.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import {
  DEFAULT_INTERFACE_PREFERENCES,
  fetchPreferences,
  savePreferences,
  subscribeEvents,
  type InterfacePreferences,
} from "@/lib/api";
import { swallowed } from "@/lib/swallowed";

interface PreferencesContextValue {
  preferences: InterfacePreferences;
  // Change one or more of them. The state is updated from the daemon's answer, so what is on
  // screen is what is stored rather than what was asked for.
  updatePreferences: (changes: Partial<InterfacePreferences>) => void;
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null);

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState<InterfacePreferences | null>(null);

  useEffect(() => {
    // Set by the teardown below, so a read that lands after this provider is gone is dropped
    // rather than setting state on nothing.
    let cancelled = false;
    const read = () => fetchPreferences()
      .then((stored) => { if (!cancelled) setPreferences(stored); })
      .catch((caught) => {
        if (!cancelled) setPreferences(DEFAULT_INTERFACE_PREFERENCES);
        swallowed({ component: "preferences", operation: "read the interface preferences" }, caught);
      });
    void read();
    // Another client changed one. The daemon says so; this rereads rather than guessing what.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "preferences_changed") void read();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  const updatePreferences = useCallback((changes: Partial<InterfacePreferences>) => {
    // Applied here first so the control the user just clicked responds now rather than after a
    // round trip, then reconciled with what the daemon actually stored.
    setPreferences((current) => (current ? { ...current, ...changes } : current));
    void savePreferences(changes)
      .then(setPreferences)
      .catch((caught) => swallowed({ component: "preferences", operation: "save an interface preference" }, caught));
  }, []);

  if (preferences === null) return null;
  return (
    <PreferencesContext.Provider value={{ preferences, updatePreferences }}>
      {children}
    </PreferencesContext.Provider>
  );
}

export function usePreferences(): PreferencesContextValue {
  const context = useContext(PreferencesContext);
  if (!context) throw new Error("usePreferences must be used inside PreferencesProvider.");
  return context;
}
