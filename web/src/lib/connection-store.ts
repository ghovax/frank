// The front-end-local store. This holds state that belongs to *this client* — which
// backends it knows how to reach and which one it used last — as opposed to the
// harness server's own history.db (sessions, transcripts, provider secrets), which
// is per-backend and travels with the server.
//
// Inside the Tauri desktop app it persists to a dedicated SQLite database
// (`sqlite:daisy.db`, tables created by the Rust migration). In a plain browser it
// falls back to localStorage so the web build keeps working with no backend of its
// own. All Tauri modules are imported dynamically and only when running under Tauri,
// so the static export and SSR prerender never touch them.

export type ConnectionKind = "local" | "remote";

export interface ConnectionProfile {
  id: string;
  name: string;
  url: string;
  kind: ConnectionKind;
  createdAt: string;
  lastUsedAt: string | null;
}

const DATABASE_NAME = "sqlite:daisy.db";
const LOCAL_STORAGE_CONNECTIONS = "daisy.connections";
const LOCAL_STORAGE_APP_STATE = "daisy.appState";
const LOCAL_STORAGE_SESSION_CONNECTIONS = "daisy.sessionConnections";

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

// Tauri SQLite backend for persisting connection profiles and app state.

type SqlDatabase = {
  select: <T>(query: string, values?: unknown[]) => Promise<T>;
  execute: (query: string, values?: unknown[]) => Promise<unknown>;
};

let databasePromise: Promise<SqlDatabase> | null = null;

async function getDatabase(): Promise<SqlDatabase> {
  if (!databasePromise) {
    databasePromise = import("@tauri-apps/plugin-sql").then((module) =>
      module.default.load(DATABASE_NAME)
    ) as Promise<SqlDatabase>;
  }
  return databasePromise;
}

// Fallback localStorage backend for plain browser environments without Tauri.

function readLocalConnections(): ConnectionProfile[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(LOCAL_STORAGE_CONNECTIONS);
    return raw ? (JSON.parse(raw) as ConnectionProfile[]) : [];
  } catch {
    return [];
  }
}

function writeLocalConnections(connections: ConnectionProfile[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOCAL_STORAGE_CONNECTIONS, JSON.stringify(connections));
  } catch {
    // best-effort
  }
}

function readLocalAppState(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(LOCAL_STORAGE_APP_STATE);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

function writeLocalAppState(state: Record<string, string>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOCAL_STORAGE_APP_STATE, JSON.stringify(state));
  } catch {
    // best-effort
  }
}

function readLocalSessionConnections(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(LOCAL_STORAGE_SESSION_CONNECTIONS);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

function writeLocalSessionConnections(state: Record<string, string>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOCAL_STORAGE_SESSION_CONNECTIONS, JSON.stringify(state));
  } catch {
    // best-effort
  }
}

// Public API for managing connection profiles and app state.

export async function listConnections(): Promise<ConnectionProfile[]> {
  if (!isTauri()) {
    return readLocalConnections().sort((a, b) =>
      (b.lastUsedAt ?? b.createdAt).localeCompare(a.lastUsedAt ?? a.createdAt)
    );
  }
  const database = await getDatabase();
  const rows = await database.select<
    { id: string; name: string; url: string; kind: string; created_at: string; last_used_at: string | null }[]
  >(
    "SELECT id, name, url, kind, created_at, last_used_at FROM connections \
     ORDER BY COALESCE(last_used_at, created_at) DESC"
  );
  return rows.map((row) => ({
    id: row.id,
    name: row.name,
    url: row.url,
    kind: (row.kind as ConnectionKind) ?? "remote",
    createdAt: row.created_at,
    lastUsedAt: row.last_used_at,
  }));
}

export async function saveConnection(profile: ConnectionProfile): Promise<void> {
  if (!isTauri()) {
    const connections = readLocalConnections().filter((entry) => entry.id !== profile.id);
    connections.push(profile);
    writeLocalConnections(connections);
    return;
  }
  const database = await getDatabase();
  await database.execute(
    "INSERT INTO connections (id, name, url, kind, created_at, last_used_at) \
     VALUES ($1, $2, $3, $4, $5, $6) \
     ON CONFLICT(id) DO UPDATE SET name = $2, url = $3, kind = $4",
    [profile.id, profile.name, profile.url, profile.kind, profile.createdAt, profile.lastUsedAt]
  );
}

export async function deleteConnection(id: string): Promise<void> {
  if (!isTauri()) {
    writeLocalConnections(readLocalConnections().filter((entry) => entry.id !== id));
    return;
  }
  const database = await getDatabase();
  await database.execute("DELETE FROM connections WHERE id = $1", [id]);
}

export async function touchConnection(id: string): Promise<void> {
  const now = new Date().toISOString();
  if (!isTauri()) {
    const connections = readLocalConnections().map((entry) =>
      entry.id === id ? { ...entry, lastUsedAt: now } : entry
    );
    writeLocalConnections(connections);
    return;
  }
  const database = await getDatabase();
  await database.execute("UPDATE connections SET last_used_at = $2 WHERE id = $1", [id, now]);
}

export async function getAppState(key: string): Promise<string | null> {
  if (!isTauri()) {
    return readLocalAppState()[key] ?? null;
  }
  const database = await getDatabase();
  const rows = await database.select<{ value: string }[]>(
    "SELECT value FROM app_state WHERE key = $1",
    [key]
  );
  return rows[0]?.value ?? null;
}

export async function setAppState(key: string, value: string): Promise<void> {
  if (!isTauri()) {
    const state = readLocalAppState();
    state[key] = value;
    writeLocalAppState(state);
    return;
  }
  const database = await getDatabase();
  await database.execute(
    "INSERT INTO app_state (key, value) VALUES ($1, $2) \
     ON CONFLICT(key) DO UPDATE SET value = $2",
    [key, value]
  );
}

export async function getSessionConnection(sessionId: string): Promise<string | null> {
  if (!sessionId) return null;
  if (!isTauri()) {
    return readLocalSessionConnections()[sessionId] ?? null;
  }
  const database = await getDatabase();
  const rows = await database.select<{ target_id: string }[]>(
    "SELECT target_id FROM session_connections WHERE session_id = $1",
    [sessionId]
  );
  return rows[0]?.target_id ?? null;
}

export async function setSessionConnection(sessionId: string, targetId: string): Promise<void> {
  if (!sessionId || !targetId) return;
  if (!isTauri()) {
    const state = readLocalSessionConnections();
    state[sessionId] = targetId;
    writeLocalSessionConnections(state);
    return;
  }
  const database = await getDatabase();
  await database.execute(
    "INSERT INTO session_connections (session_id, target_id, recorded_at) VALUES ($1, $2, $3) \
     ON CONFLICT(session_id) DO UPDATE SET target_id = $2, recorded_at = $3",
    [sessionId, targetId, new Date().toISOString()]
  );
}
