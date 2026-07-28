// The desktop app's local database, as a schema rather than as strings.
//
// This is the whole of it: small state belonging to *this client* — colour mode, locale, tray
// preference. It used to sit beside a set of saved backend profiles, and those are gone, so
// what remains is one key-value table.
//
// Declared with Drizzle rather than written as SQL by hand. The queries that read and write
// this were `SELECT value FROM app_state WHERE key = $1` and an `INSERT … ON CONFLICT` spelled
// out in a template string, which the compiler could not check and which had to be kept in
// agreement with a migration in another language by eye.

import { sqliteTable, text } from "drizzle-orm/sqlite-core";

export const appState = sqliteTable("app_state", {
  key: text("key").primaryKey(),
  value: text("value").notNull(),
});
