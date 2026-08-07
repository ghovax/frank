import type { ReactNode } from "react";

// What a settings page is made of, named where the panel and the schema-derived pages can both see it.

/** One setting as data, so the search box can match it. `stacked` puts a wide control on its own line. */
export type SettingRowDef = {
  key: string;
  title: string;
  description?: string;
  control: ReactNode;
  layout?: "row" | "stacked";
};

/** A titled group of rows, optionally followed by a full-width block of its own. */
export type SettingsPageSection = { title?: string; rows: SettingRowDef[]; block?: ReactNode };

/** One entry in the panel's left rail, and everything under it. */
export type SettingsPage = {
  id: string;
  label: string;
  icon: ReactNode;
  sections: SettingsPageSection[];
};
