/** Which machines this phone knows, which one it is talking to, and whether it can reach it. */

import * as SecureStore from "expo-secure-store";
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from "react";
import { AppState, Platform } from "react-native";

import { configure, probe } from "./api";

/** What `langmesh reach pair` encodes into its link. */
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
  /** Call a machine something else, on this phone only, since the paired name is whatever DHCP left it. */
  rename: (endpoint: string, name: string) => Promise<void>;
}

const STORAGE_KEY = "langmesh.pairings";

const ConnectionContext = createContext<ConnectionValue | null>(null);

/** A pairing code that would not do, named by which entry in `PairScreen` says so. */
export class PairingError extends Error {
  constructor(readonly reason: "notAPairingCode" | "missingTokenOrAddress") {
    super(reason);
    this.name = "PairingError";
  }
}

function isPairing(value: unknown): value is Pairing {
  if (typeof value !== "object" || value === null) return false;
  const pairing = value as Partial<Pairing>;
  return pairing.version === 1 &&
    typeof pairing.name === "string" && pairing.name.trim().length > 0 &&
    typeof pairing.token === "string" && pairing.token.length > 0 &&
    typeof pairing.endpoint === "string" && pairing.endpoint.length > 0;
}

/** Read a pairing link or a bare payload, throwing rather than returning null so the person is told why. */
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
  let payload: unknown;
  try {
    payload = JSON.parse(decoded);
  } catch {
    throw new PairingError("notAPairingCode");
  }
  if (!isPairing(payload)) {
    throw new PairingError("missingTokenOrAddress");
  }
  return {
    version: payload.version,
    name: payload.name.trim(),
    token: payload.token,
    endpoint: payload.endpoint,
  };
}

/** Secrets go to the keychain rather than ordinary storage, and the web has neither. */
const store = {
  async read(): Promise<Pairing[]> {
    const raw = Platform.OS === "web"
      ? globalThis.localStorage?.getItem(STORAGE_KEY) ?? null
      : await SecureStore.getItemAsync(STORAGE_KEY);
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.filter(isPairing) : [];
    } catch {
      // Unreadable is the same as absent, since the way out is pairing again either way.
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
    // In a browser there is nothing to probe with, because a page cannot ask whether another origin is there.
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
    // Configured before the status changes, so nothing renders as online while the API points elsewhere.
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

  // Coming back to the foreground is the moment to find out whether the machine is still reachable.
  const activeRef = useRef<Pairing | null>(null);
  useEffect(() => { activeRef.current = active; }, [active]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && activeRef.current !== null) void connect(activeRef.current);
    });
    return () => subscription.remove();
  }, [connect]);

  const add = useCallback(async (next: Pairing) => {
    // Keyed on the address, so pairing the same machine again replaces its token rather than adding one.
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
