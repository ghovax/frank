/**
 * Which languages there are, and the catalogue for each.
 *
 * In `shared/` because both clients answer this question and there is only one right answer to
 * it. It lived in `web/` and the phone grew its own copy of the same three lines within a day of
 * being written — which is the whole argument for this directory: a second list of locales is a
 * second place to forget to add one.
 *
 * Both catalogues are imported statically rather than fetched per locale. There is no server and
 * no locale routing — the desktop is a static export in a webview, the phone is a bundle — so
 * there is nothing to load them *from*, and at this size bundling both costs less than the
 * machinery for loading one.
 */

import en from "./messages/en.json";
import ja from "./messages/ja.json";

export const LOCALES = ["en", "ja"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";

// `en` is the source of truth for message shape; `ja` mirrors its keys.
export const MESSAGES: Record<Locale, typeof en> = { en, ja } as Record<Locale, typeof en>;

export function isLocale(value: string | null | undefined): value is Locale {
  return LOCALES.includes(value as Locale);
}
