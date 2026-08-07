/** Which languages there are, and the catalogue for each, shared because both clients ask. */

import en from "./messages/en.json";
import ja from "./messages/ja.json";

export const LOCALES = ["en", "ja"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";

// `en` is the source of truth for message shape; `ja` mirrors its keys.
export const MESSAGES: Record<Locale, typeof en> = { en, ja } as Record<
  Locale,
  typeof en
>;

export function isLocale(value: string | null | undefined): value is Locale {
  return LOCALES.includes(value as Locale);
}
