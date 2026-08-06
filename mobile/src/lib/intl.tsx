/** The phone's half of the one message catalogue, on the same package the desktop's provider wraps. */

import { getLocales } from "expo-localization";
import type { ReactNode } from "react";
import { IntlProvider } from "use-intl";

import { DEFAULT_LOCALE, MESSAGES, isLocale, type Locale } from "@shared/locales";

/** The catalogue this device reads, matched on language alone, falling back to the default. */
function deviceLocale(): Locale {
  for (const locale of getLocales()) {
    if (isLocale(locale.languageCode)) return locale.languageCode;
  }
  return DEFAULT_LOCALE;
}

export function Translations({ children }: { children: ReactNode }) {
  const locale = deviceLocale();
  return (
    // `timeZone` is stated because `use-intl` warns without one, and the device's is the only honest answer.
    <IntlProvider locale={locale} messages={MESSAGES[locale]} timeZone={Intl.DateTimeFormat().resolvedOptions().timeZone}>
      {children}
    </IntlProvider>
  );
}
