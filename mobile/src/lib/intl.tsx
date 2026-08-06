/** The phone's half of the one message catalogue. */

import { getLocales } from "expo-localization";
import type { ReactNode } from "react";
import { IntlProvider } from "use-intl";

import { DEFAULT_LOCALE, MESSAGES, isLocale, type Locale } from "@shared/locales";

/** The catalogue this device should read. */
function deviceLocale(): Locale {
  for (const locale of getLocales()) {
    if (isLocale(locale.languageCode)) return locale.languageCode;
  }
  return DEFAULT_LOCALE;
}

export function Translations({ children }: { children: ReactNode }) {
  const locale = deviceLocale();
  return (
    // `timeZone` is stated because `use-intl` warns without one, and the device's is the only honest answer — a phone formatting times in the machine's zone would be reporting on a place the person is not.
    <IntlProvider locale={locale} messages={MESSAGES[locale]} timeZone={Intl.DateTimeFormat().resolvedOptions().timeZone}>
      {children}
    </IntlProvider>
  );
}
