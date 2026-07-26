// Connection orchestration used by the launcher gate and the status pill: health-checking a
// backend, remembering the last target, and pointing the API client at a chosen one.
//
// Every target is reached the same way — a URL and a token, probed before use. "Local" is a
// label for the one on this machine, not a different mechanism: the app never starts a daemon,
// here or anywhere, so a local daemon that is not running is simply unreachable, exactly as a
// remote host that is not answering is.

import { setApiBase, getApiBase, invalidateDiscoveryCache, daemonStatus } from "@/lib/api";
import { isTauri, setAppState, getAppState, touchConnection, listConnections, type ConnectionKind, type ConnectionProfile } from "@/lib/connection-store";

// The conventional local daemon address. `daisyd` serves its control plane on this
// loopback port for GUI clients (the webview cannot open its unix socket), and this is
// also the API client's built-in default.
export const LOCAL_DEFAULT_URL = "http://127.0.0.1:8823";

// app_state key remembering what the user connected to last: "local" or a
// connection profile id. Drives the launcher's auto-connect on startup.
const LAST_TARGET_KEY = "last_target";
export const LOCAL_TARGET_ID = "local";

export interface ConnectionTarget {
  id: string;
  name: string;
  url: string;
  kind: ConnectionKind;
  // The token that authorises talking to this particular daemon. Empty for the local
  // one, whose token the desktop shell reads from the runtime directory itself.
  token?: string;
  sshHostAlias?: string;
  sshHostName?: string;
  sshUser?: string;
  sshPort?: number | null;
  sshIdentityFile?: string;
  sshLocalPort?: number | null;
  sshRemotePort?: number | null;
  sshContext?: string;
}

export interface SshHost {
  alias: string;
  hostName: string;
  user: string;
  port: number;
  identityFiles: string[];
}

export const LOCAL_CONNECTION_TARGET: ConnectionTarget = {
  id: LOCAL_TARGET_ID,
  name: "Local server",
  url: LOCAL_DEFAULT_URL,
  kind: "local",
};

export async function getLastTargetId(): Promise<string | null> {
  return getAppState(LAST_TARGET_KEY);
}

export async function setLastTargetId(id: string): Promise<void> {
  await setAppState(LAST_TARGET_KEY, id);
}

// Is the daemon the UI is currently talking to running on *this* machine? Only
// then do the user's local file paths mean anything to the server — which is what lets
// attachments be referenced in place instead of uploaded as bytes. An SSH/remote server
// sees a different filesystem, so its answer is false.
export async function activeConnectionIsLocal(): Promise<boolean> {
  const target = await resolveConnectionTarget(await getLastTargetId());
  return (target?.kind ?? "local") === "local";
}

function targetFromProfile(profile: ConnectionProfile): ConnectionTarget {
  return {
    id: profile.id,
    name: profile.name,
    url: profile.url,
    kind: profile.kind,
    token: profile.token,
    sshHostAlias: profile.sshHostAlias,
    sshHostName: profile.sshHostName,
    sshUser: profile.sshUser,
    sshPort: profile.sshPort,
    sshIdentityFile: profile.sshIdentityFile,
    sshLocalPort: profile.sshLocalPort,
    sshRemotePort: profile.sshRemotePort,
    sshContext: profile.sshContext,
  };
}

export async function listConnectionTargets(): Promise<ConnectionTarget[]> {
  const saved = await listConnections();
  return [
    { ...LOCAL_CONNECTION_TARGET, url: getApiBaseForLocalFallback() },
    ...saved.map(targetFromProfile),
  ];
}

function getApiBaseForLocalFallback(): string {
  return LOCAL_DEFAULT_URL;
}

export async function resolveConnectionTarget(targetId: string | null | undefined): Promise<ConnectionTarget | null> {
  if (!targetId || targetId === LOCAL_TARGET_ID) return LOCAL_CONNECTION_TARGET;
  const saved = await listConnections();
  const profile = saved.find((entry) => entry.id === targetId);
  return profile ? targetFromProfile(profile) : null;
}

// Is a daemon answering at this base URL? Probes `daemon.status`, which is the
// readiness signal: it needs no arguments and only a live, authenticated daemon answers
// it. Short timeout so the launcher stays responsive when a host is down or a tunnel
// isn't up. A stale socket file no longer looks like a running backend, which is exactly
// what the old port probe could not tell apart.
export interface ConnectionProbeOptions {
  // The token of the daemon being probed. A profile is health-checked *before* it is
  // activated, so the client's current token is the wrong one to present — without this
  // a remote daemon answers 401 and the probe reads as "host is down".
  token?: string;
  timeoutMs?: number;
}

export async function checkConnection(url: string, options: ConnectionProbeOptions = {}): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 3500);
  try {
    const status = await daemonStatus({
      apiBase: url.replace(/\/+$/, ""),
      token: options.token,
      signal: controller.signal,
    });
    return status !== null;
  } finally {
    clearTimeout(timer);
  }
}

// Where a daemon on this machine would be, and whether one is answering there.
//
// It looks; it does not start. A daemon is a program the user installs and runs, and this app
// is a client of it exactly as it is a client of a daemon on another host — the only
// difference being that this one needs no tunnel. The desktop shell knows the port the daemon
// published, so it is asked; a plain browser falls back to the conventional address.
export async function findLocalDaemon(): Promise<{ url: string; listening: boolean }> {
  if (!isTauri()) {
    return { url: LOCAL_DEFAULT_URL, listening: await checkConnection(LOCAL_DEFAULT_URL) };
  }
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<{ url: string; listening: boolean }>("local_daemon");
}

export async function listSshHosts(): Promise<SshHost[]> {
  if (!isTauri()) return [];
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<SshHost[]>("list_ssh_hosts");
}

export async function startSshTunnel(profile: Pick<ConnectionProfile, "id" | "sshHostAlias" | "sshHostName" | "sshUser" | "sshPort" | "sshIdentityFile" | "sshLocalPort" | "sshRemotePort">): Promise<string> {
  if (!isTauri()) throw new Error("SSH connections are available in the desktop app.");
  if (!profile.sshHostAlias) throw new Error("SSH host alias is required.");
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<string>("start_ssh_tunnel", {
    request: {
      profileId: profile.id,
      hostAlias: profile.sshHostAlias,
      hostName: profile.sshHostName || undefined,
      user: profile.sshUser || undefined,
      port: profile.sshPort || undefined,
      identityFile: profile.sshIdentityFile || undefined,
      localPort: profile.sshLocalPort || undefined,
      remotePort: profile.sshRemotePort || 8823,
    },
  });
}

// Wait for a freshly-started daemon to accept requests, polling until it responds or
// the overall budget runs out (the frozen binary takes a few seconds to boot).
export async function waitForConnection(
  url: string,
  options: ConnectionProbeOptions & { totalMs?: number } = {},
): Promise<boolean> {
  const deadline = Date.now() + (options.totalMs ?? 20000);
  while (Date.now() < deadline) {
    if (await checkConnection(url, { token: options.token, timeoutMs: 1500 })) return true;
    await new Promise((resolve) => setTimeout(resolve, 600));
  }
  return false;
}

// Point the whole UI at a backend: its address and the token that authorises it, which
// travel together because they belong to the same daemon. Persists the choice (so it
// survives reloads), clears the discovery cache, and records it for next launch.
//
// The local daemon's token is empty here on purpose — the desktop shell reads it from
// this machine's runtime directory, which is the one place it is authoritative.
export async function activateConnection(
  url: string,
  targetId: string,
  options: { token?: string; profileId?: string } = {},
): Promise<void> {
  setApiBase(url, options.token ?? "");
  invalidateDiscoveryCache();
  await setLastTargetId(targetId);
  if (options.profileId) {
    await touchConnection(options.profileId).catch(() => {});
  }
}

export async function activateConnectionTarget(target: ConnectionTarget): Promise<void> {
  const local = target.kind === "local";
  await activateConnection(target.url, target.id, {
    token: local ? "" : target.token ?? "",
    profileId: local ? undefined : target.id,
  });
}

export async function resolveReachableConnectionUrl(target: ConnectionTarget): Promise<string> {
  if (target.kind === "local") return (await findLocalDaemon()).url;
  if (target.kind === "ssh") return startSshTunnel({
    id: target.id,
    sshHostAlias: target.sshHostAlias,
    sshHostName: target.sshHostName,
    sshUser: target.sshUser,
    sshPort: target.sshPort,
    sshIdentityFile: target.sshIdentityFile,
    sshLocalPort: target.sshLocalPort,
    sshRemotePort: target.sshRemotePort,
  });
  return target.url;
}

export { getApiBase };
