"use client";

// What the interface remembers about itself, held for the render, with the daemon as the source of truth.

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
  // Change one or more of them, updating from the daemon's answer rather than from the request.
  updatePreferences: (changes: Partial<InterfacePreferences>) => void;
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null);

export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [preferences, setPreferences] = useState<InterfacePreferences | null>(null);

  useEffect(() => {
    // Set by the teardown below, so a read landing after this provider is gone is dropped.
    let cancelled = false;
    const read = () =>
      fetchPreferences()
        .then((stored) => {
          if (!cancelled) setPreferences(stored);
        })
        .catch((caught) => {
          if (!cancelled) setPreferences(DEFAULT_INTERFACE_PREFERENCES);
          swallowed(
            { component: "preferences", operation: "read the interface preferences" },
            caught,
          );
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
    // Applied here first so the control responds now, then reconciled with what the daemon stored.
    setPreferences((current) => (current ? { ...current, ...changes } : current));
    void savePreferences(changes)
      .then(setPreferences)
      .catch((caught) =>
        swallowed({ component: "preferences", operation: "save an interface preference" }, caught),
      );
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
