// Small persistent state belonging to *this client* — the colour mode, the chosen locale, the
// tray preference. Not the daemon's history.db, which holds sessions, transcripts and provider
// secrets and travels with the daemon.
//
// This used to live in `connection-store.ts` alongside a set of saved backend profiles. Those
// are gone: a location already says where work runs — this machine, or an SSH host — so a
// separate notion of "which backend am I connected to" was a second answer to a question
// already answered, one layer up, where it could disagree.
//
// Inside the Tauri desktop app it persists to SQLite through Drizzle, whose schema lives in
// `schema.ts`. The queries here were hand-written SQL in template strings, unchecked by the
// compiler and kept in agreement with a Rust migration by eye. In a plain browser there is no
// SQLite, so it falls back to localStorage and the web build works with no desktop shell.
// Tauri modules are imported dynamically and only when running under Tauri, so the static
// export and the SSR prerender never touch them.

import { eq } from "drizzle-orm";
import { drizzle, type SqliteRemoteDatabase } from "drizzle-orm/sqlite-proxy";
import { appState } from "./schema";

const DATABASE_NAME = "sqlite:internal.db";
const LOCAL_STORAGE_APP_STATE = "frank.appState";

export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

type SqlDatabase = {
  select: <T>(query: string, values?: unknown[]) => Promise<T>;
  execute: (query: string, values?: unknown[]) => Promise<unknown>;
};

let databasePromise: Promise<SqliteRemoteDatabase<Record<string, never>>> | null = null;

// Drizzle talks to whatever this callback talks to, which is the Tauri SQL plugin. The proxy
// driver exists for exactly this: a database reached over someone else's transport.
//
// It wants each row as an array of values in the order the query selected them, while the
// plugin hands back objects — hence `Object.values`. That is safe here because the plugin
// preserves column order, and it is the documented shape for this driver.
function connect(): Promise<SqliteRemoteDatabase<Record<string, never>>> {
  if (!databasePromise) {
    databasePromise = import("@tauri-apps/plugin-sql")
      .then((module) => module.default.load(DATABASE_NAME) as Promise<SqlDatabase>)
      .then((database) =>
        drizzle(async (sql, params, method) => {
          if (method === "run") {
            await database.execute(sql, params);
            return { rows: [] };
          }
          const rows = await database.select<Record<string, unknown>[]>(sql, params);
          const values = rows.map((row) => Object.values(row));
          // `get` asks for one row, not a list of them.
          return { rows: method === "get" ? (values[0] ?? []) : values };
        })
      );
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
  const database = await connect();
  const rows = await database
    .select({ value: appState.value })
    .from(appState)
    .where(eq(appState.key, key));
  return rows[0]?.value ?? null;
}

export async function setAppState(key: string, value: string): Promise<void> {
  if (!isTauri()) {
    const state = readLocalAppState();
    state[key] = value;
    writeLocalAppState(state);
    return;
  }
  const database = await connect();
  await database
    .insert(appState)
    .values({ key, value })
    .onConflictDoUpdate({ target: appState.key, set: { value } });
}
