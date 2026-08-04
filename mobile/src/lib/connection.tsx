/**
 * Which machines this phone knows, which one it is talking to, and whether it can reach it.
 *
 * Plural, and that is the shape of the whole file. A phone that can reach one Frank is a phone
 * pointed at a desk; the point of reaching a machine over a tailnet is that there may be several
 * of them and you are not next to any of them. So a pairing is not *the* pairing — it is one
 * entry in a set, and choosing between them is an ordinary thing to do rather than a repair.
 *
 * A machine is identified by its address. One `frank reach` is one endpoint, so pairing the same
 * machine twice updates the entry it already has rather than growing a second one with a stale
 * token — which is what makes re-pairing after `frank reach rotate` do the obvious thing.
 *
 * Nothing is raced or discovered. The address a machine gives is its name on the tailnet, and
 * that does not change; an earlier version carried a ranked list of addresses and tried them in
 * turn, which was machinery for coping with addresses that stop working.
 */

import * as SecureStore from "expo-secure-store";
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from "react";
import { AppState, Platform } from "react-native";

import { configure, probe } from "./api";

/** What `frank reach pair` encodes into its link. */
export interface Pairing {
  version: number;
  name: string;
  token: string;
  /** The machine's address on the tailnet, e.g. `https://mac.tailnet.ts.net`. */
  endpoint: string;
}

export type ConnectionStatus =
  /** No machine is chosen. The list is what is on screen. */
  | "idle"
  /** Asking the chosen machine whether it is there. */
  | "connecting"
  /** It answered and the token was accepted. */
  | "online"
  /** It did not answer. The pairing is still good; the machine is asleep, or off the tailnet. */
  | "offline"
  /** It answered and refused the token — the machine rotated it, or this device was unpaired. */
  | "rejected";

interface ConnectionValue {
  /** Every machine this phone has been paired with, in the order they were added. */
  machines: Pairing[];
  /** The one being talked to, or `null` when the list is what is on screen. */
  active: Pairing | null;
  status: ConnectionStatus;
  /** Remember a machine, and go to it. Replaces an entry with the same address. */
  add: (pairing: Pairing) => Promise<void>;
  /** Go to one already known. */
  select: (endpoint: string) => void;
  /** Stop talking to whichever machine is active, without forgetting it. */
  leave: () => void;
  /** Forget a machine and its token. */
  forget: (endpoint: string) => Promise<void>;
  /** Ask the active machine again whether it is there. */
  reconnect: () => void;
  /**
   * Call a machine something else, on this phone only.
   *
   * The pairing arrives carrying the host's own name, which is whatever DHCP and the ISP left it
   * — `Giovannis-MBP`, and worse on some networks. That is a fine default and a poor label, and
   * the machine is not the right place to fix it: the name is what *this* phone calls it, and
   * another device pairing with the same Mac may reasonably call it something else.
   */
  rename: (endpoint: string, name: string) => Promise<void>;
}

const STORAGE_KEY = "frank.pairings";

const ConnectionContext = createContext<ConnectionValue | null>(null);

/** A pairing code that would not do, named by which entry in `PairScreen` says so. */
export class PairingError extends Error {
  constructor(readonly reason: "notAPairingCode" | "missingTokenOrAddress") {
    super(reason);
    this.name = "PairingError";
  }
}

/**
 * Read a `frank://pair#…` link, or a bare base64 payload pasted out of one.
 *
 * Throws rather than returning null: every caller is a person who just scanned or pasted
 * something, and being told which way it was wrong is the only useful thing to say.
 *
 * What it throws is a key into `PairScreen`, not a sentence. This is a plain module with no hook
 * to reach the catalogue through, and an English sentence written here would be one no screen
 * could translate — the language of a message belongs to whatever is about to show it.
 */
export function parsePairing(input: string): Pairing {
  const trimmed = input.trim();
  const fragment = trimmed.includes("#") ? trimmed.slice(trimmed.indexOf("#") + 1) : trimmed;
  let decoded: string;
  try {
    // The encoder strips `=`; `atob` wants the padding back.
    const padded = fragment.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((fragment.length + 3) % 4);
    decoded = globalThis.atob(padded);
  } catch {
    throw new PairingError("notAPairingCode");
  }
  let payload: Pairing;
  try {
    payload = JSON.parse(decoded) as Pairing;
  } catch {
    throw new PairingError("notAPairingCode");
  }
  if (!payload?.token || !payload?.endpoint) {
    throw new PairingError("missingTokenOrAddress");
  }
  return {
    version: Number(payload.version ?? 1),
    name: String(payload.name ?? "Frank"),
    token: String(payload.token),
    endpoint: String(payload.endpoint),
  };
}

/**
 * Secrets go to the keychain, not to AsyncStorage — these are bearer tokens with full control of
 * somebody's laptop. On web there is no keychain, and `expo-secure-store` says so by being
 * unavailable; the browser build is a development surface, so it falls back to `localStorage`
 * rather than refusing to run.
 */
const store = {
  async read(): Promise<Pairing[]> {
    const raw = Platform.OS === "web"
      ? globalThis.localStorage?.getItem(STORAGE_KEY) ?? null
      : await SecureStore.getItemAsync(STORAGE_KEY);
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? (parsed as Pairing[]).filter((entry) => entry?.endpoint && entry?.token) : [];
    } catch {
      // Unreadable is the same as absent: there is nothing to salvage from a corrupt token, and
      // the way out — pair again — is the same either way.
      return [];
    }
  },
  async write(machines: Pairing[]): Promise<void> {
    const raw = JSON.stringify(machines);
    if (Platform.OS === "web") {
      globalThis.localStorage?.setItem(STORAGE_KEY, raw);
      return;
    }
    await SecureStore.setItemAsync(STORAGE_KEY, raw, {
      keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
    });
  },
};

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [machines, setMachines] = useState<Pairing[]>([]);
  const [active, setActive] = useState<Pairing | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const attempt = useRef<AbortController | null>(null);

  const connect = useCallback(async (machine: Pairing) => {
    attempt.current?.abort();
    const controller = new AbortController();
    attempt.current = controller;
    setStatus("connecting");
    // In a browser there is nothing to probe with.
    //
    // A page cannot ask whether another origin is there: the request is cross-origin, the reach
    // listener answers no `access-control-allow-origin` because it is not meant to be scripted
    // from arbitrary pages, and what comes back is an opaque failure indistinguishable from a
    // machine that is asleep. Reporting that as "not answering" was worse than useless — it sent
    // people to go and wake a machine that was wide awake.
    //
    // So on web the pairing is taken at its word and the browser is left to find out, which it
    // does perfectly well: opening the endpoint either shows Frank or shows the browser's own
    // "cannot connect", and that is a truthful answer arrived at by something that is actually
    // allowed to ask.
    const answer = Platform.OS === "web"
      ? "ok"
      : await probe(machine.endpoint, machine.token, controller.signal);
    if (controller.signal.aborted) return;
    if (answer === "unreachable") {
      setStatus("offline");
      return;
    }
    if (answer === "unauthorized") {
      setStatus("rejected");
      return;
    }
    // Configured before the status changes, so nothing renders as "online" with the API still
    // pointing at the previous machine.
    configure(machine.endpoint, machine.token);
    setStatus("online");
  }, []);

  useEffect(() => {
    let cancelled = false;
    store.read()
      .then((found) => { if (!cancelled) setMachines(found); })
      .catch(() => { if (!cancelled) setMachines([]); });
    return () => { cancelled = true; };
  }, []);

  // A phone that has been in a pocket has had its streams dropped and possibly its network
  // changed. Coming back to the foreground is the moment to find out whether the machine it was
  // talking to is still there, and it costs one request when the answer is yes.
  const activeRef = useRef<Pairing | null>(null);
  useEffect(() => { activeRef.current = active; }, [active]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && activeRef.current !== null) void connect(activeRef.current);
    });
    return () => subscription.remove();
  }, [connect]);

  const add = useCallback(async (next: Pairing) => {
    // Keyed on the address, so pairing the same machine again replaces its token rather than
    // leaving a second entry holding one that no longer works.
    const merged = [...machines.filter((entry) => entry.endpoint !== next.endpoint), next];
    await store.write(merged);
    setMachines(merged);
    setActive(next);
    await connect(next);
  }, [machines, connect]);

  const select = useCallback((endpoint: string) => {
    const machine = machines.find((entry) => entry.endpoint === endpoint);
    if (machine === undefined) return;
    setActive(machine);
    void connect(machine);
  }, [machines, connect]);

  const leave = useCallback(() => {
    attempt.current?.abort();
    configure("", "");
    setActive(null);
    setStatus("idle");
  }, []);

  const forget = useCallback(async (endpoint: string) => {
    const remaining = machines.filter((entry) => entry.endpoint !== endpoint);
    await store.write(remaining);
    setMachines(remaining);
    if (activeRef.current?.endpoint === endpoint) leave();
  }, [machines, leave]);

  const reconnect = useCallback(() => {
    if (activeRef.current !== null) void connect(activeRef.current);
  }, [connect]);

  const rename = useCallback(async (endpoint: string, name: string) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    const renamed = machines.map((entry) => (entry.endpoint === endpoint ? { ...entry, name: trimmed } : entry));
    await store.write(renamed);
    setMachines(renamed);
    setActive((current) => (current?.endpoint === endpoint ? { ...current, name: trimmed } : current));
  }, [machines]);

  const value = useMemo<ConnectionValue>(
    () => ({ machines, active, status, add, select, leave, forget, reconnect, rename }),
    [machines, active, status, add, select, leave, forget, reconnect, rename],
  );

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

export function useConnection(): ConnectionValue {
  const value = useContext(ConnectionContext);
  if (value === null) throw new Error("useConnection was called outside ConnectionProvider.");
  return value;
}
