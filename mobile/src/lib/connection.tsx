/**
 * Which Frank this phone talks to, and whether it can reach it right now.
 *
 * Pairing hands over a small payload: a name, a token, and a list of addresses the machine
 * answers on, best first. The list is the interesting part. A phone is at home on Wi-Fi in the
 * evening and on a mobile network in the morning, and no single address is right for both — the
 * LAN one is the fastest when it works and useless when it does not, and the tailnet one works
 * from anywhere and is a slower path to a machine three metres away.
 *
 * So the app does not pick an address once. It **races** them on every connect, in the order the
 * pairing gave, and keeps whichever answered. That is what makes an endpoint feel stable without
 * anything having a fixed IP: the address changes and the connection does not.
 */

import * as SecureStore from "expo-secure-store";
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from "react";
import { AppState, Platform } from "react-native";

import { configure, probe } from "./api";

/** What `frank reach pair` encodes into its QR code. */
export interface Pairing {
  version: number;
  name: string;
  token: string;
  endpoints: string[];
}

export type ConnectionStatus =
  /** Nothing has been paired, so there is nothing to connect to. */
  | "unpaired"
  /** Racing the endpoints. */
  | "connecting"
  /** One of them answered and the token was accepted. */
  | "online"
  /** None of them answered. The pairing is still good; the machine is asleep, or away. */
  | "offline"
  /** One answered and refused the token — the machine rotated it, or this device was unpaired. */
  | "rejected";

interface ConnectionValue {
  status: ConnectionStatus;
  pairing: Pairing | null;
  /** The address that answered, once one has. */
  endpoint: string;
  pair: (pairing: Pairing) => Promise<void>;
  unpair: () => Promise<void>;
  reconnect: () => void;
  /** Call this machine something else on this phone. */
  rename: (name: string) => Promise<void>;
}

const STORAGE_KEY = "frank.pairing";

const ConnectionContext = createContext<ConnectionValue | null>(null);

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
/** A pairing code that would not do, named by which entry in `PairScreen` says so. */
export class PairingError extends Error {
  constructor(readonly reason: "notAPairingCode" | "missingTokenOrAddress" | "noAddress") {
    super(reason);
    this.name = "PairingError";
  }
}

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
  if (!payload?.token || !Array.isArray(payload.endpoints)) {
    throw new PairingError("missingTokenOrAddress");
  }
  if (payload.endpoints.length === 0) {
    throw new PairingError("noAddress");
  }
  return {
    version: Number(payload.version ?? 1),
    name: String(payload.name ?? "Frank"),
    token: String(payload.token),
    endpoints: payload.endpoints.map(String),
  };
}

/**
 * The first endpoint that answers, or `null`.
 *
 * Raced rather than tried in turn: the whole point of carrying several addresses is that the
 * unreachable ones fail by timing out, and three timeouts in sequence is a quarter of a minute
 * staring at a spinner. `Promise.any` over the whole list settles as fast as the fastest.
 * `unauthorized` is not a win but it is an *answer*, so it is remembered separately — a machine
 * that refuses the token has more to say than one that is simply not there.
 */
async function raceEndpoints(
  endpoints: string[], token: string, signal: AbortSignal,
): Promise<{ endpoint: string } | { rejected: true } | null> {
  let sawRejection = false;
  const attempts = endpoints.map(async (candidate) => {
    const answer = await probe(candidate, token, signal);
    if (answer === "unauthorized") {
      sawRejection = true;
      throw new Error("unauthorized");
    }
    if (answer !== "ok") throw new Error("unreachable");
    return candidate;
  });
  try {
    return { endpoint: await Promise.any(attempts) };
  } catch {
    return sawRejection ? { rejected: true } : null;
  }
}

/**
 * Secrets go to the keychain, not to AsyncStorage — this payload is a bearer token with full
 * control of somebody's laptop. On web there is no keychain, and `expo-secure-store` says so by
 * being unavailable; the browser build is a development surface, so it falls back to
 * `localStorage` rather than refusing to run.
 */
const store = {
  async get(): Promise<string | null> {
    if (Platform.OS === "web") return globalThis.localStorage?.getItem(STORAGE_KEY) ?? null;
    return SecureStore.getItemAsync(STORAGE_KEY);
  },
  async set(value: string): Promise<void> {
    if (Platform.OS === "web") {
      globalThis.localStorage?.setItem(STORAGE_KEY, value);
      return;
    }
    await SecureStore.setItemAsync(STORAGE_KEY, value, {
      keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
    });
  },
  async clear(): Promise<void> {
    if (Platform.OS === "web") {
      globalThis.localStorage?.removeItem(STORAGE_KEY);
      return;
    }
    await SecureStore.deleteItemAsync(STORAGE_KEY);
  },
};

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [endpoint, setEndpoint] = useState("");
  const attempt = useRef<AbortController | null>(null);

  const connect = useCallback(async (current: Pairing | null) => {
    attempt.current?.abort();
    if (current === null) {
      setStatus("unpaired");
      setEndpoint("");
      return;
    }
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
    // allowed to ask. Widening CORS to buy back a probe would mean letting any page a person
    // visits script a listener holding a token with full control of their machine.
    const won = Platform.OS === "web"
      ? { endpoint: current.endpoints[0] ?? "" }
      : await raceEndpoints(current.endpoints, current.token, controller.signal);
    if (controller.signal.aborted) return;
    if (won === null) {
      setStatus("offline");
      return;
    }
    if ("rejected" in won) {
      setStatus("rejected");
      return;
    }
    // Configured before the status changes, so nothing renders as "online" with the API still
    // pointing at the previous machine.
    configure(won.endpoint, current.token);
    setEndpoint(won.endpoint);
    setStatus("online");
  }, []);

  useEffect(() => {
    let cancelled = false;
    store.get()
      .then((stored) => {
        if (cancelled) return;
        const found = stored ? (JSON.parse(stored) as Pairing) : null;
        setPairing(found);
        void connect(found);
      })
      .catch(() => {
        if (!cancelled) setStatus("unpaired");
      });
    return () => { cancelled = true; };
  }, [connect]);

  // A phone that has been in a pocket has had its streams dropped and possibly its network
  // changed. Coming back to the foreground is the moment to find out which endpoint works now,
  // and it costs one request when the answer is "the same one".
  const pairingRef = useRef<Pairing | null>(null);
  useEffect(() => { pairingRef.current = pairing; }, [pairing]);
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && pairingRef.current !== null) void connect(pairingRef.current);
    });
    return () => subscription.remove();
  }, [connect]);

  const pair = useCallback(async (next: Pairing) => {
    await store.set(JSON.stringify(next));
    setPairing(next);
    await connect(next);
  }, [connect]);

  const unpair = useCallback(async () => {
    attempt.current?.abort();
    await store.clear();
    configure("", "");
    setPairing(null);
    setEndpoint("");
    setStatus("unpaired");
  }, []);

  const reconnect = useCallback(() => { void connect(pairingRef.current); }, [connect]);

  /**
   * Rename the machine, on this phone only.
   *
   * The pairing arrives carrying the host's own name, which is whatever DHCP and the ISP left
   * it — `Giovannis-MBP`, and worse on some networks. That is a fine default and a poor label,
   * and the machine is not the right place to fix it: the name is what *this* phone calls it,
   * and another device pairing with the same Mac may reasonably call it something else.
   */
  const rename = useCallback(async (name: string) => {
    const current = pairingRef.current;
    const trimmed = name.trim();
    if (current === null || !trimmed || trimmed === current.name) return;
    const renamed = { ...current, name: trimmed };
    await store.set(JSON.stringify(renamed));
    setPairing(renamed);
  }, []);

  const value = useMemo<ConnectionValue>(
    () => ({ status, pairing, endpoint, pair, unpair, reconnect, rename }),
    [status, pairing, endpoint, pair, unpair, reconnect, rename],
  );

  return <ConnectionContext.Provider value={value}>{children}</ConnectionContext.Provider>;
}

export function useConnection(): ConnectionValue {
  const value = useContext(ConnectionContext);
  if (value === null) throw new Error("useConnection was called outside ConnectionProvider.");
  return value;
}
