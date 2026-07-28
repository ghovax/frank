// Small persistent state belonging to *this client* — the colour mode, the chosen locale,
// the tray preference. Not the daemon's history.db, which holds sessions, transcripts and
// provider secrets and travels with the daemon.
//
// This used to live in `connection-store.ts` alongside a set of saved backend profiles. Those
// are gone: a location already says where work runs — this machine, or an SSH host — so a
// separate notion of "which backend am I connected to" was a second answer to a question
// already answered, one layer up, where it could disagree. The interface talks to the local
// daemon, and reaching elsewhere is a property of the folder, not of the whole application.
//
// Inside the Tauri desktop app this persists to a dedicated SQLite database
// (`sqlite:internal.db`, tables created by the Rust migration). In a plain browser it falls
// back to localStorage so the web build works with no desktop shell. Tauri modules are
// imported dynamically and only when running under Tauri, so the static export and the SSR
// prerender never touch them.

const DATABASE_NAME = "sqlite:internal.db";
const LOCAL_STORAGE_APP_STATE = "frank.appState";

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

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

function readLocalAppState(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(LOCAL_STORAGE_APP_STATE);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    // A quota-exceeded or disabled-storage browser reads as "nothing saved", which is the
    // right answer here: every caller has a working default.
    return {};
  }
}

function writeLocalAppState(state: Record<string, string>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOCAL_STORAGE_APP_STATE, JSON.stringify(state));
  } catch {
    // Best-effort: a preference that fails to persist is a preference that resets, which is
    // not worth interrupting anyone over.
  }
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
