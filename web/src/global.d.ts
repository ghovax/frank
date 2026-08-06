// Catalogue keys checked against en.json, so a typo or a missing key is a compile error.
import type en from "../../shared/messages/en.json";

declare module "next-intl" {
  interface AppConfig {
    Messages: typeof en;
  }
}
