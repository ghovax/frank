import type { ReactNode } from "react";

// What a settings page is made of, named where both the panel and the pages generated from the
// configuration schema can see it. The panel defined these for itself, which was right while it
// was the only thing building pages; the schema-derived ones are the second builder, and a
// second copy of the shape is how two builders start drifting.

/** One setting modeled as data, so the search box can match it: a title and optional
 * description on the left, a `control` on the right. `stacked` puts a wide control (an API-key
 * field, a list of paths) on its own line beneath the label instead. */
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
export type SettingsPage = { id: string; label: string; icon: ReactNode; sections: SettingsPageSection[] };
