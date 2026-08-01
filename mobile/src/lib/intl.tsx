/**
 * The phone's half of the one message catalogue.
 *
 * `use-intl` rather than a translation layer of this app's own, because it *is* `next-intl` —
 * the desktop's provider is a thin Next-shaped wrapper over exactly this package. So the two
 * clients read the same `shared/messages/*.json`, call the same `useTranslations(namespace)`,
 * and write the same ICU placeholders. A second convention here would be a second place for a
 * string to be missing from.
 *
 * There is no locale switch. The device's language is the answer a phone already has, and a
 * setting to override it is a setting the desktop does not have either.
 */

import { getLocales } from "expo-localization";
import type { ReactNode } from "react";
import { IntlProvider } from "use-intl";

import { DEFAULT_LOCALE, MESSAGES, isLocale, type Locale } from "@shared/locales";

/**
 * The catalogue this device should read.
 *
 * Matched on the language alone: `ja-JP` and `ja` want the same Japanese, and a list keyed by
 * region would need an entry for every region before it answered any of them. Anything with no
 * catalogue falls to the default, which is the source language rather than a preference.
 */
function deviceLocale(): Locale {
  for (const locale of getLocales()) {
    if (isLocale(locale.languageCode)) return locale.languageCode;
  }
  return DEFAULT_LOCALE;
}

export function Translations({ children }: { children: ReactNode }) {
  const locale = deviceLocale();
  return (
    // `timeZone` is stated because `use-intl` warns without one, and the device's is the only
    // honest answer — a phone formatting times in the machine's zone would be reporting on a
    // place the person is not.
    <IntlProvider locale={locale} messages={MESSAGES[locale]} timeZone={Intl.DateTimeFormat().resolvedOptions().timeZone}>
      {children}
    </IntlProvider>
  );
}
