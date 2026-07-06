// Connection orchestration used by the launcher gate and the status pill:
// health-checking a backend, starting/stopping the bundled local server (via the
// Rust commands), remembering the last target, and pointing the API client at a
// chosen backend.

import { setApiBase, getApiBase, invalidateDiscoveryCache } from "@/lib/api";
import { isTauri, setAppState, getAppState, touchConnection, listConnections, type ConnectionKind, type ConnectionProfile } from "@/lib/connection-store";

// The conventional local harness address. The bundled server binds here, and this
// is also the API client's built-in default.
export const LOCAL_DEFAULT_URL = "http://localhost:8822";

// app_state key remembering what the user connected to last: "local" or a
// connection profile id. Drives the launcher's auto-connect on startup.
const LAST_TARGET_KEY = "last_target";
export const LOCAL_TARGET_ID = "local";

export interface ConnectionTarget {
  id: string;
  name: string;
  url: string;
  kind: ConnectionKind;
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
  name: "Local",
  url: LOCAL_DEFAULT_URL,
  kind: "local",
};

export async function getLastTargetId(): Promise<string | null> {
  return getAppState(LAST_TARGET_KEY);
}

export async function setLastTargetId(id: string): Promise<void> {
  await setAppState(LAST_TARGET_KEY, id);
}

export async function listConnectionTargets(): Promise<ConnectionTarget[]> {
  const saved = await listConnections();
  return [
    { ...LOCAL_CONNECTION_TARGET, url: getApiBaseForLocalFallback() },
    ...saved.map((profile) => ({
      id: profile.id,
      name: profile.name,
      url: profile.url,
      kind: profile.kind,
      sshHostAlias: profile.sshHostAlias,
      sshHostName: profile.sshHostName,
      sshUser: profile.sshUser,
      sshPort: profile.sshPort,
      sshIdentityFile: profile.sshIdentityFile,
      sshLocalPort: profile.sshLocalPort,
      sshRemotePort: profile.sshRemotePort,
      sshContext: profile.sshContext,
    })),
  ];
}

function getApiBaseForLocalFallback(): string {
  return LOCAL_DEFAULT_URL;
}

export async function resolveConnectionTarget(targetId: string | null | undefined): Promise<ConnectionTarget | null> {
  if (!targetId || targetId === LOCAL_TARGET_ID) return LOCAL_CONNECTION_TARGET;
  const saved = await listConnections();
  const profile = saved.find((entry) => entry.id === targetId);
  return profile
    ? {
      id: profile.id,
      name: profile.name,
      url: profile.url,
      kind: profile.kind,
      sshHostAlias: profile.sshHostAlias,
      sshHostName: profile.sshHostName,
      sshUser: profile.sshUser,
      sshPort: profile.sshPort,
      sshIdentityFile: profile.sshIdentityFile,
      sshLocalPort: profile.sshLocalPort,
      sshRemotePort: profile.sshRemotePort,
      sshContext: profile.sshContext,
    }
    : null;
}

// Is a harness server answering at this base URL? Hits `/home`, which every harness
// server exposes and which needs no arguments. Short timeout so the launcher stays
// responsive when a host is down or a tunnel isn't up.
export async function checkConnection(url: string, timeoutMs = 3500): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${url.replace(/\/+$/, "")}/home`, {
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// Ensure the local server is up and return its URL. In the desktop app this spawns
// the bundled server if nothing is listening yet; in a plain browser it just points
// at the conventional local address (the user runs the server themselves).
export async function startLocalServer(): Promise<string> {
  if (!isTauri()) return LOCAL_DEFAULT_URL;
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<string>("start_local_server");
}

export async function stopLocalServer(): Promise<void> {
  if (!isTauri()) return;
  const { invoke } = await import("@tauri-apps/api/core");
  await invoke("stop_local_server");
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
      remotePort: profile.sshRemotePort || 8822,
    },
  });
}

// Wait for a freshly-started server to accept requests, polling until it responds
// or the overall budget runs out (the frozen server takes a few seconds to boot).
export async function waitForConnection(url: string, totalMs = 20000): Promise<boolean> {
  const deadline = Date.now() + totalMs;
  while (Date.now() < deadline) {
    if (await checkConnection(url, 1500)) return true;
    await new Promise((resolve) => setTimeout(resolve, 600));
  }
  return false;
}

// Point the whole UI at a backend. Persists the address (so it survives reloads),
// clears the discovery cache, and records the choice for next launch.
export async function activateConnection(url: string, targetId: string, profileId?: string): Promise<void> {
  setApiBase(url);
  invalidateDiscoveryCache();
  await setLastTargetId(targetId);
  if (profileId) {
    await touchConnection(profileId).catch(() => {});
  }
}

export async function activateConnectionTarget(target: ConnectionTarget): Promise<void> {
  await activateConnection(target.url, target.id, target.kind === "local" ? undefined : target.id);
}

export async function resolveReachableConnectionUrl(target: ConnectionTarget): Promise<string> {
  if (target.kind === "local") return startLocalServer();
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
