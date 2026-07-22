// Type-safe messages: `useTranslations`/`translation()` keys are checked against en.json (the source
// of truth), so a typo or a key missing from the catalog is a compile error across the app.
import type en from "../messages/en.json";

declare module "next-intl" {
  interface AppConfig {
    Messages: typeof en;
  }
}
