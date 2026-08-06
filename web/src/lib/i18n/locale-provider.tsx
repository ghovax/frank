"use client";

// Runtime locale for the app. Modeled on color-mode.tsx: reads the locale the daemon has
// stored and re-renders the whole tree with the matching messages when it changes — no
// navigation, no route change, because locale here is a setting, not a URL segment. This is
// the client-only next-intl wiring that a static export (output: "export", no
// server/middleware) requires.

import * as React from "react";
import { NextIntlClientProvider } from "next-intl";
import { usePreferences } from "@/lib/preferences";
import { DEFAULT_LOCALE, MESSAGES, isLocale, type Locale } from "@shared/locales";

interface LocaleContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
}

const LocaleContext = React.createContext<LocaleContextValue | null>(null);

export function LocaleProvider({ children }: React.PropsWithChildren) {
  // The daemon's answer is the state: change the language in the desktop app and a browser
  // window looking at the same daemon follows, with nothing here to keep in step.
  const { preferences, updatePreferences } = usePreferences();
  const stored = preferences.locale;
  const locale: Locale = isLocale(stored) ? stored : DEFAULT_LOCALE;

  // Keep <html lang> accurate for a11y (the root layout hardcodes lang="en").
  React.useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = React.useCallback((next: Locale) => {
    updatePreferences({ locale: next });
  }, [updatePreferences]);

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
  const context = React.useContext(LocaleContext);
  if (!context) return { locale: DEFAULT_LOCALE, setLocale: () => {} };
  return context;
}
