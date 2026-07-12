CREATE TABLE IF NOT EXISTS session_connections (
  session_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
