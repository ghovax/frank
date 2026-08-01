// The app's message catalogs. This is a static-export Tauri app (no server, no i18n
// routing), so both locales are bundled and imported at build time; the active locale is
// an in-app setting (see locale-provider) and messages are swapped client-side.
// `shared/`, not `web/`: the phone shows the same words, and a second catalogue is how
// "Medium risk" on one client becomes "medium" on the other.
import en from "../../../../shared/messages/en.json";
import ja from "../../../../shared/messages/ja.json";

export const LOCALES = ["en", "ja"] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = "en";

// `en` is the source of truth for message shape; `ja` mirrors its keys.
export const MESSAGES: Record<Locale, typeof en> = { en, ja } as Record<Locale, typeof en>;

export function isLocale(value: string | null | undefined): value is Locale {
  return value === "en" || value === "ja";
}
