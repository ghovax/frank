"use client";

// Runtime locale for the app. Modeled on color-mode.tsx: reads the persisted locale on
// mount (Tauri SQLite app_state, or localStorage in a plain browser) and re-renders the
// whole tree with the matching messages when it changes — no navigation, no route change,
// because locale here is a setting, not a URL segment. This is the client-only next-intl
// wiring that a static export (output: "export", no server/middleware) requires.

import * as React from "react";
import { NextIntlClientProvider } from "next-intl";
import { getAppState, isTauri, setAppState } from "@/lib/app-state";
import { DEFAULT_LOCALE, MESSAGES, isLocale, type Locale } from "./messages";

const LOCALE_KEY = "locale";
const LOCALE_LS_KEY = "frank.locale";

interface LocaleContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
}

const LocaleContext = React.createContext<LocaleContextValue | null>(null);

export function LocaleProvider({ children }: React.PropsWithChildren) {
  const [locale, setLocaleState] = React.useState<Locale>(() => {
    // Plain browser: hydrate synchronously from localStorage to avoid a flash of English.
    if (typeof window === "undefined" || isTauri()) return DEFAULT_LOCALE;
    try {
      const stored = window.localStorage.getItem(LOCALE_LS_KEY);
      if (isLocale(stored)) return stored;
    } catch {
      /* localStorage may be unavailable */
    }
    return DEFAULT_LOCALE;
  });

  // Tauri: the SQLite app_state read is async, so load after mount.
  React.useEffect(() => {
    if (!isTauri()) return;
    let cancelled = false;
    getAppState(LOCALE_KEY)
      .then((stored) => {
        if (!cancelled && isLocale(stored)) setLocaleState(stored);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Keep <html lang> accurate for a11y (the root layout hardcodes lang="en").
  React.useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = React.useCallback((next: Locale) => {
    setLocaleState(next);
    if (isTauri()) {
      void setAppState(LOCALE_KEY, next);
      return;
    }
    try {
      window.localStorage.setItem(LOCALE_LS_KEY, next);
    } catch {
      /* best-effort */
    }
  }, []);

  const value = React.useMemo(() => ({ locale, setLocale }), [locale, setLocale]);

  // A single stable `now` per mount for relativeTime; the device time zone is correct for
  // a desktop app. Both are passed explicitly so static rendering is warning-free.
  const now = React.useMemo(() => new Date(), []);
  const timeZone =
    typeof Intl !== "undefined" ? Intl.DateTimeFormat().resolvedOptions().timeZone : "UTC";

  return (
    <LocaleContext.Provider value={value}>
      <NextIntlClientProvider locale={locale} messages={MESSAGES[locale]} timeZone={timeZone} now={now}>
        {children}
      </NextIntlClientProvider>
    </LocaleContext.Provider>
  );
}

export function useLocale(): LocaleContextValue {
  const ctx = React.useContext(LocaleContext);
  if (!ctx) return { locale: DEFAULT_LOCALE, setLocale: () => {} };
  return ctx;
}
