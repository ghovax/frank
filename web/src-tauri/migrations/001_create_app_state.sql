-- Small client-side state: colour mode, locale, tray preference. Its shape is declared in
-- `web/src/lib/schema.ts` and reached through Drizzle rather than hand-written SQL.
CREATE TABLE IF NOT EXISTS app_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
