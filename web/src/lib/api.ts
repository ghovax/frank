// Where the daemon lives, and the capability token that proves we may talk to it.
import { setFaultSender, swallowed } from "./swallowed";

export const LOCAL_DAEMON_PORT = 8824;
export const LOCAL_DAEMON_URL = `http://127.0.0.1:${LOCAL_DAEMON_PORT}`;

const DEFAULT_API_BASE =
  (typeof process !== "undefined" ? process.env.NEXT_PUBLIC_API_BASE : "") || LOCAL_DAEMON_URL;

// The token a development page presents, and only ever a development page.
const DEVELOPMENT_TOKEN =
  typeof process !== "undefined" && process.env.NODE_ENV !== "production"
    ? process.env.NEXT_PUBLIC_TOKEN || ""
    : "";

function runningInTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

let API_BASE = DEFAULT_API_BASE;

// The token for the daemon we are talking to. The local one authenticates nothing on a remote host.
let localDaemonToken = DEVELOPMENT_TOKEN;
let daemonEndpointPromise: Promise<void> | null = null;

async function resolveDaemonEndpoint(): Promise<void> {
  if (!runningInTauri()) {
    // Served by `langmesh web`? That proxies the daemon at its own origin, so the base is the empty string.
    try {
      const response = await fetch("/__langmesh/runtime.json", { cache: "no-store" });
      if (response.ok) {
        const runtime = await response.json();
        if (runtime?.proxied) API_BASE = "";
      }
    } catch {
      // Not served by it; nothing to learn.
    }
    return;
  }
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const endpoint = await invoke<{ url: string; token: string }>("daemon_endpoint");
    if (endpoint?.url) API_BASE = endpoint.url.replace(/\/+$/, "");
    localDaemonToken = endpoint?.token ?? "";
  } catch {
    // No endpoint reported: leave the defaults, and let the failing request surface it.
  }
}

// Memoized for the life of a daemon rather than of the page — see `forgetDaemonEndpoint`.
function ensureDaemonEndpoint(): Promise<void> {
  if (!daemonEndpointPromise) daemonEndpointPromise = resolveDaemonEndpoint();
  return daemonEndpointPromise;
}

/** Drop the memoized endpoint, since a daemon mints a fresh token and port at every boot. */
export function forgetDaemonEndpoint(): void {
  daemonEndpointPromise = null;
}

export interface ApiRequestOptions {
  apiBase?: string;
  // The token to present when the request is aimed at a daemon other than the active one.
  token?: string;
}

function apiBase(options?: ApiRequestOptions): string {
  return (options?.apiBase || API_BASE).replace(/\/+$/, "");
}

function apiUrl(path: string, options?: ApiRequestOptions): string {
  return `${apiBase(options)}${path}`;
}

function requestToken(options?: ApiRequestOptions): string {
  return options?.token ?? localDaemonToken;
}

// The token as a query parameter, for transports that carry no header: a websocket, an iframe, a download.
function withDaemonToken(url: string, options?: ApiRequestOptions): string {
  const token = requestToken(options);
  if (!token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

function websocketUrl(path: string, options?: ApiRequestOptions): string {
  // An empty base means this page is proxied at its own origin, and a websocket URL must be absolute.
  const base = apiBase(options) || (typeof window !== "undefined" ? window.location.origin : "");
  const url = new URL(path, `${base}/`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return withDaemonToken(url.toString(), options);
}

// The one door every request goes through, so the token is attached in exactly one place.
async function apiFetch(
  path: string,
  options: RequestInit & ApiRequestOptions = {},
): Promise<Response> {
  const { apiBase: baseOverride, token: tokenOverride, headers, ...request } = options;
  await ensureDaemonEndpoint();
  const token = requestToken({ token: tokenOverride });
  return fetch(apiUrl(path, { apiBase: baseOverride }), {
    ...request,
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(headers as Record<string, string> | undefined),
    },
  });
}

// The daemon's control plane: one POST of `{method, params}`, relayed to the session for data-plane calls.
async function rpc<T>(
  method: string,
  params: Record<string, unknown> = {},
  options: ApiRequestOptions & { signal?: AbortSignal } = {},
): Promise<T> {
  const response = await apiFetch("/rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method, params }),
    apiBase: options.apiBase,
    signal: options.signal,
  });
  const payload = (await response.json().catch(() => ({}))) as {
    result?: T;
    error?: { code?: string; message?: string };
  };
  // The daemon's own message names what went wrong, which is worth more than the status code.
  if (!response.ok || payload.error) {
    throw new Error(payload.error?.message || `${method} failed (${response.status})`);
  }
  return payload.result as T;
}

// The address the client is currently talking to.

// Async, because the token must be resolved first: a websocket URL without one is refused at the handshake.
export async function terminalWebSocketUrl(
  options: {
    sessionId?: string | null;
    workingDirectory?: string;
    terminalKey?: string;
    locationKind?: string;
    locationBaseDirectory?: string;
    locationHostAlias?: string;
    rows?: number;
    columns?: number;
  } = {},
): Promise<string> {
  await ensureDaemonEndpoint();
  const params = new URLSearchParams();
  if (options.sessionId) params.set("session_id", options.sessionId);
  if (options.workingDirectory) params.set("working_directory", options.workingDirectory);
  if (options.terminalKey) params.set("terminal_key", options.terminalKey);
  if (options.locationKind) params.set("location_kind", options.locationKind);
  if (options.locationBaseDirectory)
    params.set("location_base_directory", options.locationBaseDirectory);
  if (options.locationHostAlias) params.set("location_host_alias", options.locationHostAlias);
  if (options.rows) params.set("rows", String(options.rows));
  if (options.columns) params.set("columns", String(options.columns));
  const query = params.toString();
  return websocketUrl(`/terminal${query ? `?${query}` : ""}`);
}

export interface TerminalInfo {
  terminalKey: string;
  cwd: string;
  running: boolean;
}

function terminalContextQuery(
  sessionId: string | null | undefined,
  workingDirectory: string | undefined,
): string {
  const params = new URLSearchParams();
  if (sessionId) params.set("session_id", sessionId);
  if (workingDirectory) params.set("working_directory", workingDirectory);
  const query = params.toString();
  return query ? `?${query}` : "";
}

export async function listTerminals(
  sessionId: string | null,
  workingDirectory: string,
): Promise<TerminalInfo[]> {
  const response = await apiFetch(`/terminals${terminalContextQuery(sessionId, workingDirectory)}`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.terminals)
    ? (data.terminals as Array<{ terminal_key: string; cwd?: string; running?: boolean }>).map(
        (entry) => ({
          terminalKey: entry.terminal_key,
          cwd: entry.cwd ?? "",
          running: Boolean(entry.running),
        }),
      )
    : [];
}

export async function deleteTerminal(
  sessionId: string | null,
  workingDirectory: string,
  terminalKey: string,
): Promise<void> {
  if (!terminalKey) return;
  await apiFetch(
    `/terminals/${encodeURIComponent(terminalKey)}${terminalContextQuery(sessionId, workingDirectory)}`,
    {
      method: "DELETE",
    },
  );
}

// Point the client at a daemon with the token authorising that one, and persist the choice.

type CacheEntry = {
  expiresAt: number;
  data?: unknown;
  promise?: Promise<unknown>;
};

const DISCOVERY_CACHE_TTL_MS = 15_000;
const discoveryCache = new Map<string, CacheEntry>();

function discoveryKey(path: string, workingDirectory?: string): string {
  return `${path}?working_directory=${workingDirectory ?? ""}`;
}

async function fetchJson<T>(path: string, options?: ApiRequestOptions): Promise<T> {
  const response = await apiFetch(path, options);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function cachedDiscovery<T>(key: string, loader: () => Promise<T>): Promise<T> {
  const now = Date.now();
  const cached = discoveryCache.get(key);
  if (cached?.promise) return cached.promise as Promise<T>;
  if (cached && cached.expiresAt > now) return Promise.resolve(cached.data as T);

  const promise = loader()
    .then((data) => {
      discoveryCache.set(key, {
        expiresAt: Date.now() + DISCOVERY_CACHE_TTL_MS,
        data,
      });
      return data;
    })
    .catch((error) => {
      discoveryCache.delete(key);
      throw error;
    });
  discoveryCache.set(key, { expiresAt: 0, promise });
  return promise;
}

export function invalidateDiscoveryCache(): void {
  discoveryCache.clear();
}

// The URL serving a local file for display, each segment encoded and the slashes kept.
export function localFileUrl(path: string): string {
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  return withDaemonToken(`${API_BASE}/files/${encoded}`);
}

// A generic uploaded file: the core knows the stored file and its metadata, and nothing more.
export interface Attachment {
  upload_id: string;
  title: string;
  filename: string;
  path: string;
  mime_type: string;
  size: number;
  sha256: string;
}

export async function uploadFile(file: File): Promise<Attachment> {
  const body = new FormData();
  body.append("file", file);
  const response = await apiFetch(`/uploads`, {
    method: "POST",
    body,
  });
  if (!response.ok) throw new Error(`Failed to upload ${file.name} (${response.status})`);
  return (await response.json()) as Attachment;
}

// Register an attachment by path with no copy, which is valid only when server and file share a machine.
export async function referenceAttachment(path: string): Promise<Attachment> {
  const response = await apiFetch(`/attachments/reference`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) throw new Error(`Failed to attach ${path} (${response.status})`);
  return (await response.json()) as Attachment;
}

// Workspaces, locations, and the SSH host registry.

// A connectable SSH host from ~/.ssh/config (the source of truth for remotes).
export interface SshHost {
  alias: string;
  hostname: string;
  user: string;
  port: number;
  identity_files: string[];
}

// A named place a workspace runs tools in. `name` is derived from the connection, not entered.
export interface Location {
  id: string;
  workspace_id: string;
  name: string;
  kind: "local" | "remote";
  host_alias: string;
  host_known: boolean;
  base_directory: string;
  uri: string;
  permission_mode: string;
  created_at: string;
}

export interface Workspace {
  id: string;
  created_at: string;
  updated_at: string;
  session_count: number;
  locations?: Location[];
  // The conversation this workspace was last opened at. The daemon's memory, not the browser's.
  last_session_id?: string;
}

// The editable shape of a location (create/update). No `name` — the server derives it.
export interface LocationInput {
  kind: "local" | "remote";
  base_directory: string;
  host_alias?: string;
  permission_mode?: string;
}

export interface WorkspaceCreateInput {
  locations: LocationInput[];
}

/** Another LangMesh this one can reach, without its token, so a rendered list carries no credential. */
export interface Machine {
  id: string;
  name: string;
  endpoint: string;
  created_at: string;
}

export async function listMachines(): Promise<Machine[]> {
  const response = await apiFetch(`/machines`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.machines) ? (data.machines as Machine[]) : [];
}

/** Remember a machine from the `langmesh://pair#…` link `langmesh reach` prints. */
export async function addMachine(link: string): Promise<Machine> {
  const response = await apiFetch(`/machines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ link }),
  });
  if (!response.ok) {
    let detail = "";
    try {
      detail = String((await response.json())?.detail ?? "");
    } catch {
      // A body that is not JSON says nothing the status did not.
    }
    throw new Error(detail || `Could not add that machine (${response.status}).`);
  }
  return (await response.json()) as Machine;
}

/** The URL that opens a machine, token and all, asked for at the moment somebody chooses to go. */
export async function machineAddress(machineId: string): Promise<string> {
  const response = await apiFetch(`/machines/${encodeURIComponent(machineId)}/address`);
  if (!response.ok) throw new Error(`Could not open that machine (${response.status}).`);
  return String((await response.json())?.url ?? "");
}

export async function renameMachine(machineId: string, name: string): Promise<void> {
  await apiFetch(`/machines/${encodeURIComponent(machineId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function forgetMachine(machineId: string): Promise<void> {
  await apiFetch(`/machines/${encodeURIComponent(machineId)}`, { method: "DELETE" });
}

export async function listSshHosts(): Promise<SshHost[]> {
  const response = await apiFetch(`/hosts`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.hosts) ? (data.hosts as SshHost[]) : [];
}

export async function listWorkspaces(): Promise<Workspace[]> {
  const response = await apiFetch(`/workspaces`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.workspaces) ? (data.workspaces as Workspace[]) : [];
}

export async function getWorkspace(workspaceId: string): Promise<Workspace | null> {
  const response = await apiFetch(`/workspaces/${encodeURIComponent(workspaceId)}`);
  if (!response.ok) return null;
  return (await response.json()) as Workspace;
}

export async function createWorkspace(input: WorkspaceCreateInput): Promise<Workspace> {
  const response = await apiFetch(`/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to create workspace (${response.status})`);
  return (await response.json()) as Workspace;
}

/** Remember which conversation a workspace is open at, on the server, since every client shares it. */
export async function rememberLastSession(workspaceId: string, sessionId: string): Promise<void> {
  await apiFetch(`/workspaces/${encodeURIComponent(workspaceId)}/last-session`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => undefined);
}

export async function deleteWorkspace(workspaceId: string): Promise<void> {
  await apiFetch(`/workspaces/${encodeURIComponent(workspaceId)}`, { method: "DELETE" });
}

export async function createLocation(workspaceId: string, input: LocationInput): Promise<Location> {
  const response = await apiFetch(`/workspaces/${encodeURIComponent(workspaceId)}/locations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to add location (${response.status})`);
  return (await response.json()) as Location;
}

export async function updateLocation(locationId: string, input: LocationInput): Promise<Location> {
  const response = await apiFetch(`/locations/${encodeURIComponent(locationId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to update location (${response.status})`);
  return (await response.json()) as Location;
}

export async function deleteLocation(locationId: string): Promise<void> {
  await apiFetch(`/locations/${encodeURIComponent(locationId)}`, { method: "DELETE" });
}

// External A2A agents: peers on other hosts, reached by message rather than created here.

export interface RemoteAgent {
  name: string;
  cardUrl: string;
  enabled: boolean;
  authType: string;
  allowedProfiles: string[];
  allowedHosts: string[];
  allowPrivate: boolean;
  cardTtlSeconds: number;
  health: string; // unresolved | ok | unreachable | untrusted
  error: string;
  resolvedName: string;
  resolvedDescription: string;
  skills: string[];
}

export interface RemoteAgentAuthInput {
  type: string; // none | bearer | api_key | oauth2
  token?: string;
  header?: string;
  schemePrefix?: string;
  tokenUrl?: string;
  clientId?: string;
  clientSecret?: string;
  scopes?: string[];
}

export interface RemoteAgentInput {
  name: string;
  cardUrl: string;
  enabled?: boolean;
  auth?: RemoteAgentAuthInput;
  cardTtlSeconds?: number;
  allowedHosts?: string[];
  allowPrivate?: boolean;
  allowedProfiles?: string[];
}

export interface Schedule {
  id: string;
  workspace_id: string;
  name: string;
  cron: string;
  timezone: string;
  agent: string;
  prompt: string;
  permission_mode: PermissionMode;
  working_directory: string;
  enabled: boolean;
  last_fired_at: string;
  last_session_id: string;
  last_error: string;
  created_at: string;
  // Worked out on every read rather than stored, so editing the cron line cannot leave it stale.
  next_firing: string;
}

export interface ScheduleInput {
  workspace_id: string;
  name: string;
  cron: string;
  prompt: string;
  agent: string;
  // No default: a schedule runs unwatched, so the daemon refuses one that does not state its mode.
  permission_mode: PermissionMode;
  timezone: string;
  working_directory: string;
}

export async function listSchedules(workspaceId: string): Promise<Schedule[]> {
  const response = await apiFetch(`/schedules?workspace_id=${encodeURIComponent(workspaceId)}`);
  if (!response.ok) throw new Error(`Failed to list schedules (${response.status})`);
  const data = (await response.json()) as { schedules: Schedule[] };
  return data.schedules;
}

export async function createSchedule(input: ScheduleInput): Promise<Schedule> {
  const response = await apiFetch(`/schedules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) {
    // The daemon's own sentence, rather than a status code somebody then has to look up.
    const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(detail?.detail || `Failed to create schedule (${response.status})`);
  }
  return (await response.json()) as Schedule;
}

export async function setScheduleEnabled(scheduleId: string, enabled: boolean): Promise<Schedule> {
  const response = await apiFetch(`/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) throw new Error(`Failed to update schedule (${response.status})`);
  return (await response.json()) as Schedule;
}

export async function deleteSchedule(scheduleId: string): Promise<void> {
  await apiFetch(`/schedules/${encodeURIComponent(scheduleId)}`, { method: "DELETE" });
}

export async function runSchedule(scheduleId: string): Promise<Schedule> {
  const response = await apiFetch(`/schedules/${encodeURIComponent(scheduleId)}/run`, {
    method: "POST",
  });
  if (!response.ok) throw new Error(`Failed to run schedule (${response.status})`);
  return (await response.json()) as Schedule;
}

export async function listRemoteAgents(): Promise<RemoteAgent[]> {
  const response = await apiFetch(`/remote-agents`);
  if (!response.ok) throw new Error(`Failed to list remote agents (${response.status})`);
  const data = (await response.json()) as { agents: RemoteAgent[] };
  return data.agents ?? [];
}

export async function upsertRemoteAgent(input: RemoteAgentInput): Promise<void> {
  const response = await apiFetch(`/remote-agents/${encodeURIComponent(input.name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to save remote agent (${response.status})`);
}

export async function deleteRemoteAgent(name: string): Promise<void> {
  await apiFetch(`/remote-agents/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function refreshRemoteAgent(name: string): Promise<{ health: string; error: string }> {
  const response = await apiFetch(`/remote-agents/${encodeURIComponent(name)}/refresh`, {
    method: "POST",
  });
  if (!response.ok) throw new Error(`Failed to refresh remote agent (${response.status})`);
  return (await response.json()) as { health: string; error: string };
}

// The A2A convention: an extension's attributes sit under one URI-namespaced metadata key.
export const METADATA_KEY = "urn:langmesh:ext:turn:v1";
export const CONTENT_BLOCK_METADATA_KEY = "urn:langmesh:ext:content-block:v1";

export type PermissionMode = "ask" | "automatic";
export type WorktreeStrategy = "none" | "branch" | "worktree";

export interface AgentSummary {
  id: string;
  name: string;
  title?: string;
  // What the agent is for — shown as the subtitle in the agent picker.
  description?: string;
  // The agent's resolved `provider/model`. Empty means it has no runnable model configured.
  model?: string;
}

export interface AgentBashConfiguration {
  enabled: boolean;
  background_allowed: boolean;
  permissions: Record<string, string>;
}

export interface AgentConfiguration {
  id: string;
  name: string;
  title: string;
  model: string;
  provider: string;
  reasoning_effort: string;
  /** The loosest mode this agent allows, or `null` where it sets no ceiling — which most cards mean. */
  permission_mode: PermissionMode | null;
  tools_enabled: string[];
  tools_disabled: string[];
  bash: AgentBashConfiguration;
  path: string;
}

export interface SaveAgentConfigurationPayload {
  model?: string;
  provider?: string;
  reasoning_effort?: string;
  // `null` clears the ceiling; omitted leaves it as it was.
  permission_mode?: PermissionMode | null;
  tools_enabled?: string[];
  tools_disabled?: string[];
  bash?: Partial<AgentBashConfiguration>;
}

// Agents are scoped to the folder: bundled, then home, then that folder's own, deduped.
export async function fetchAgents(workingDirectory?: string): Promise<AgentSummary[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const data = await cachedDiscovery(discoveryKey("/agents", workingDirectory), () =>
    fetchJson<{ agents: AgentSummary[] }>(`/agents${query}`),
  );
  return data.agents;
}

export async function fetchAgentConfiguration(
  agent: string,
  workingDirectory?: string,
): Promise<AgentConfiguration> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  return fetchJson<AgentConfiguration>(
    `/agents/${encodeURIComponent(agent)}/configuration${query}`,
  );
}

export async function saveAgentConfiguration(
  agent: string,
  payload: SaveAgentConfigurationPayload,
  workingDirectory?: string,
): Promise<AgentConfiguration> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const response = await apiFetch(`/agents/${encodeURIComponent(agent)}/configuration${query}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`Failed to save agent configuration (${response.status})`);
  invalidateDiscoveryCache();
  return (await response.json()) as AgentConfiguration;
}

export interface AgentSkill {
  id: string;
  name?: string;
  title?: string;
  description?: string;
  tags?: string[];
  examples?: string[];
  enabled?: boolean;
  // "global" (from ~/.agents) or "workspace" (from the selected folder's .agents).
  scope?: "global" | "workspace";
}

export interface AgentCard {
  name: string;
  title?: string;
  description?: string;
  url: string;
  version?: string;
  skills: AgentSkill[];
}

// Every served agent's card and its skills, scoped to the workspace path the same way.
export async function fetchAgentCards(workingDirectory?: string): Promise<AgentCard[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const data = await cachedDiscovery(discoveryKey("/agents/cards", workingDirectory), () =>
    fetchJson<{ cards?: AgentCard[] }>(`/agents/cards${query}`),
  );
  return data.cards ?? [];
}

export interface ProviderCredential {
  api_key: string;
  base_url: string;
}

export interface AttachmentSettings {
  // The ceiling on an image inlined into a conversation, since a huge one rides in it forever.
  inline_image_megabytes: number;
}

export interface CompactionSettings {
  // Reclaiming context on its own as it fills (manual compaction always works).
  automatic: boolean;
  reclaim_at_fraction: number;
  output_reserve_fraction: number;
  recent_working_set_fraction: number;
  verbatim_user_fraction: number;
}

export interface Settings {
  permission_mode: PermissionMode;
  exa_api_key: string;
  composio_api_key: string;
  // Web-fetch engines for fetch_url. Both empty is valid, since Jina runs keyless.
  jina_api_key: string;
  firecrawl_api_key: string;
  // Optional proxy for the web-fetch direct tier and file downloads (IP-blocked sites).
  web_fetch_proxy_url: string;
  // What a session's tool children may do, enforced by the operating system and edited in the file.
  sandbox: SandboxSettings;
  // What this machine can enforce with. Empty means the toggle cannot be honoured, which must be said.
  sandbox_backend: { backend: string; detail: string };
  // Opt-in: a snapshot of the user's machine habits in the system prompt. Off by default.
  user_context_enabled: boolean;
  // Opt-in: let the agent control macOS apps via the computer-use tool. Off by default.
  computer_control_enabled: boolean;
  // Whether sessions may install tools, and whether this machine can offer it at all.
  toolbox_enabled: boolean;
  toolbox_available: boolean;
  dictation_enabled: boolean;
  worktree_strategy: "none" | "branch" | "worktree";
  compaction: CompactionSettings;
  attachments: AttachmentSettings;
  providers: Record<string, ProviderCredential>;
}

// Only reached when the settings request fails, and `required` matches the harness's own default.
const DEFAULT_SANDBOX: SandboxSettings = {
  enforce: "required",
  network: true,
  filesystem: { readable: [], writable: [], deny: [], grantable: [] },
  limits: {},
  umask: null,
  nice: 0,
};

const DEFAULT_ATTACHMENTS: AttachmentSettings = { inline_image_megabytes: 20 };

const DEFAULT_COMPACTION: CompactionSettings = {
  automatic: false,
  reclaim_at_fraction: 0.85,
  output_reserve_fraction: 0.1,
  recent_working_set_fraction: 0.25,
  verbatim_user_fraction: 0.1,
};

// Persist the context-reclaiming settings (automatic on/off, pruning, and the thresholds).
export async function updateCompactionSettings(
  changes: Partial<CompactionSettings>,
): Promise<void> {
  await apiFetch(`/settings/compaction`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

// Persist the attachment limits, which each turn reads live.
export async function updateAttachmentSettings(
  changes: Partial<AttachmentSettings>,
): Promise<void> {
  await apiFetch(`/settings/attachments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

// Toggle the opt-in user-context snapshot in the system prompt (rebuilds runtimes).
export async function updateUserContextSetting(enabled: boolean): Promise<void> {
  await apiFetch(`/settings/user-context`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// Every setting the schema defines, with what it holds and what it is set to — and no words.
export type SettingKind =
  "boolean" | "integer" | "number" | "string" | "choice" | "list" | "map" | "section";

export interface SettingEntry {
  // The dotted path it is written under, and the key its label and description are looked up by.
  path: string;
  kind: SettingKind;
  choices: string[];
  optional: boolean;
  // A credential, as the field declares it, so masking follows the schema rather than the spelling.
  secret: boolean;
  default: unknown;
  value: unknown;
  // Whether the file says this, as opposed to the code shipping it.
  configured: boolean;
}

export interface SettingsSectionSchema {
  path: string;
  settings: SettingEntry[];
}

export async function fetchSettingsSchema(): Promise<SettingsSectionSchema[]> {
  const response = await apiFetch(`/settings/schema`);
  if (!response.ok) return [];
  const data = (await response.json()) as { sections?: SettingsSectionSchema[] };
  return data.sections ?? [];
}

// Set one setting. The server validates first, so a refusal is of the value and its message is what to read.
export async function updateSettingValue(path: string, value: unknown): Promise<void> {
  const response = await apiFetch(`/settings/value`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, value }),
  });
  if (!response.ok) throw new Error(await refusalText(response));
}

// Put a setting back by removing it, which is not the same as writing today's default.
export async function resetSettingValue(path: string): Promise<void> {
  const response = await apiFetch(`/settings/value?path=${encodeURIComponent(path)}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error(await refusalText(response));
}

// What the server said when it would not take a value, since it is the thing that knows.
async function refusalText(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
  } catch {
    // A refusal that is not JSON is still a refusal; fall through to the status.
  }
  return `${response.status}`;
}

// Toggle whether each session gets a tool profile of its own to install into (rebuilds runtimes).
export async function updateToolboxSetting(enabled: boolean): Promise<void> {
  await apiFetch(`/settings/toolbox`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// Toggle the opt-in computer-use tool that controls macOS apps (rebuilds runtimes).
export async function updateComputerControlSetting(enabled: boolean): Promise<void> {
  await apiFetch(`/settings/computer-control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// Dictation state. `loading` is real rather than a wait: the first fetch is about a gigabyte.
export type DictationState = "idle" | "loading" | "ready" | "failed";

export interface DictationStatus {
  enabled: boolean;
  model: string;
  state: DictationState;
  // Why loading failed, in a sentence, when it did.
  failure: string;
}

// `prepare` starts the model loading too, so the weights arrive before somebody reaches for the microphone.
export async function fetchDictationStatus(prepare = false): Promise<DictationStatus> {
  const response = await apiFetch(`/dictation${prepare ? "?prepare=true" : ""}`);
  const data = await response.json();
  return {
    enabled: !!data.enabled,
    model: String(data.model ?? ""),
    state: (data.state ?? "idle") as DictationState,
    failure: String(data.failure ?? ""),
  };
}

// Opt in or out of dictation. Turning it off also releases the model this machine was holding.
export async function updateDictationSetting(enabled: boolean): Promise<void> {
  await apiFetch(`/settings/dictation`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// Transcribe one recording locally. The body is raw mono float32, so neither side needs a codec.
export async function transcribeDictation(samples: Float32Array): Promise<string> {
  const response = await apiFetch(`/dictation/transcribe`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: samples.buffer.slice(
      samples.byteOffset,
      samples.byteOffset + samples.byteLength,
    ) as ArrayBuffer,
  });
  if (!response.ok) {
    // Lift the reason out: the server knows whether it was a failed download or a missing package.
    let detail = "";
    try {
      detail = String((await response.json())?.detail ?? "");
    } catch {
      // A body that is not JSON says nothing more than the status already did.
    }
    throw new Error(detail || `Transcription failed (${response.status}).`);
  }
  const data = await response.json();
  return String(data.text ?? "");
}

// Whether the server can read Full-Disk-Access data. False on any error, including non-macOS.
export async function fetchFullDiskAccess(): Promise<boolean> {
  try {
    const response = await apiFetch(`/system/full-disk-access`);
    if (!response.ok) return false;
    return (await response.json()).granted === true;
  } catch (caught) {
    swallowed({ component: "api", operation: "read the Full Disk Access state" }, caught);
    return false;
  }
}

// Open System Settings to the Full Disk Access pane so the user can add LangMesh in one hop.
export async function openFullDiskAccessSettings(): Promise<void> {
  await apiFetch(`/system/full-disk-access/open`, { method: "POST" }).catch((caught) =>
    swallowed({ component: "api", operation: "open the Full Disk Access pane" }, caught),
  );
}

// Whether the app can control other apps, which the computer-use tool needs. False on any error.
export async function fetchAccessibility(): Promise<boolean> {
  try {
    const response = await apiFetch(`/system/accessibility`);
    if (!response.ok) return false;
    return (await response.json()).granted === true;
  } catch (caught) {
    swallowed({ component: "api", operation: "read the Accessibility state" }, caught);
    return false;
  }
}

// Trigger the system Accessibility prompt and open its pane so the user can grant LangMesh.
export async function openAccessibilitySettings(): Promise<void> {
  await apiFetch(`/system/accessibility/open`, { method: "POST" }).catch((caught) =>
    swallowed({ component: "api", operation: "open the Accessibility pane" }, caught),
  );
}

// Restart the daemon to pick up a new Accessibility grant, then reload the window against it.
export async function restartDaemon(): Promise<{ sessions_slept: number }> {
  const result = await rpc<{ restarting: boolean; sessions_slept: number }>("daemon.restart", {});
  return { sessions_slept: result?.sessions_slept ?? 0 };
}

// Quit and relaunch the desktop app, so the webview reconnects to whatever daemon is listening.
export async function restartApp(): Promise<void> {
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("restart_app");
  } catch {
    // Not running inside Tauri (dev in a browser) — nothing to restart.
  }
}

export interface ModelOption {
  id: string;
  name: string;
  provider: string;
  available: boolean;
  // Capabilities from models.dev: `attachment` gates the attach button, the rest annotate the picker.
  attachment?: boolean;
  vision?: boolean;
  input_modalities?: string[];
  // ISO release date, or "" if unknown. The picker orders newest-first on this.
  release_date?: string;
}

export interface ProviderOption {
  id: string;
  name: string;
  openai_compatible: boolean;
  credential_id: string;
}

export interface ModelsResponse {
  models: ModelOption[];
  providers: ProviderOption[];
}

// How the interface looks and where it opens, held by the daemon so every client is one LangMesh.
export interface InterfacePreferences {
  color_mode: "system" | "light" | "dark";
  locale: string;
  last_workspace_id: string;
  computer_control_awaiting_grant: boolean;
}

export const DEFAULT_INTERFACE_PREFERENCES: InterfacePreferences = {
  color_mode: "system",
  locale: "",
  last_workspace_id: "",
  computer_control_awaiting_grant: false,
};

export async function fetchPreferences(): Promise<InterfacePreferences> {
  const response = await apiFetch(`/preferences`);
  // Every one has a default, so an unreachable daemon reads as "nothing chosen yet" rather than an error.
  if (!response.ok) return DEFAULT_INTERFACE_PREFERENCES;
  return (await response.json()) as InterfacePreferences;
}

// Only the fields being changed are sent, and the whole of what is now stored comes back.
export async function savePreferences(
  changes: Partial<InterfacePreferences>,
): Promise<InterfacePreferences> {
  const response = await apiFetch(`/preferences`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  if (!response.ok) throw new Error("Could not save the preference.");
  return (await response.json()) as InterfacePreferences;
}

// API credentials stored in the daemon's configuration.yaml (under $XDG_CONFIG_HOME/langmesh).
export async function fetchSettings(): Promise<Settings> {
  const response = await apiFetch(`/settings`);
  if (!response.ok) {
    return {
      permission_mode: "ask",
      exa_api_key: "",
      composio_api_key: "",
      jina_api_key: "",
      firecrawl_api_key: "",
      web_fetch_proxy_url: "",
      sandbox: DEFAULT_SANDBOX,
      sandbox_backend: { backend: "", detail: "" },
      user_context_enabled: false,
      computer_control_enabled: false,
      toolbox_enabled: false,
      toolbox_available: false,
      dictation_enabled: false,
      worktree_strategy: "none",
      compaction: DEFAULT_COMPACTION,
      attachments: DEFAULT_ATTACHMENTS,
      providers: {},
    };
  }
  return (await response.json()) as Settings;
}

export interface SaveSettingsPayload {
  permission_mode?: PermissionMode;
  sandbox?: Partial<SandboxSettings>;
  exa_api_key?: string;
  composio_api_key?: string;
  jina_api_key?: string;
  firecrawl_api_key?: string;
  web_fetch_proxy_url?: string;
  provider_keys?: Record<string, string>;
  provider_base_urls?: Record<string, string>;
  worktree_strategy?: "none" | "branch" | "worktree";
}

export async function saveSettings(settings: SaveSettingsPayload): Promise<void> {
  await apiFetch(`/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

// The model catalog for the picker, with availability, and the provider registry.
export async function fetchModels(): Promise<ModelsResponse> {
  const response = await apiFetch(`/models`);
  if (!response.ok) return { models: [], providers: [] };
  return response.json();
}

// Recently selected models, newest first, so the picker surfaces what a person actually switches between.
export interface RecentModel {
  id: string;
  name: string;
  provider: string;
}

export async function fetchRecentModels(): Promise<RecentModel[]> {
  const response = await apiFetch(`/models/recent`);
  if (!response.ok) return [];
  const data = await response.json();
  return data.models ?? [];
}

// ChatGPT sign-in state: an OAuth session held server-side, reported as present and for which account.
export interface ChatGPTUsageWindow {
  key: string;
  used_percent: number;
  window_minutes: number;
  resets_at: number | null;
}

// The account's usage, from headers on the last turn, so there is no cheaper source to poll.
export interface ChatGPTUsage {
  plan_type: string;
  active_limit: string;
  captured_at: number;
  credits: { has_credits: boolean; balance: number | null; unlimited: boolean };
  windows: ChatGPTUsageWindow[];
}

export interface ChatGPTAuthStatus {
  signed_in: boolean;
  email: string;
  usage?: ChatGPTUsage | null;
}

export async function fetchChatGPTAuthStatus(): Promise<ChatGPTAuthStatus> {
  const response = await apiFetch(`/auth/chatgpt`);
  if (!response.ok) return { signed_in: false, email: "", usage: null };
  return response.json();
}

// Begin sign-in: the server binds its callback and returns the authorize URL to open.
export async function startChatGPTLogin(): Promise<{ authorize_url: string }> {
  const response = await apiFetch(`/auth/chatgpt/start`, { method: "POST" });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || "Could not start ChatGPT sign-in.");
  }
  return response.json();
}

export async function signOutChatGPT(): Promise<void> {
  await apiFetch(`/auth/chatgpt`, { method: "DELETE" });
}

// Cursor sign-in state, without the usage meters: its service reports no remaining allowance.
export interface CursorAuthStatus {
  signed_in: boolean;
  account: string;
}

export async function fetchCursorAuthStatus(): Promise<CursorAuthStatus> {
  const response = await apiFetch(`/auth/cursor`);
  if (!response.ok) return { signed_in: false, account: "" };
  return response.json();
}

// Begin sign-in: no redirect lands here, so completion arrives on a broadcast or by re-polling.
export async function startCursorLogin(): Promise<{ authorize_url: string }> {
  const response = await apiFetch(`/auth/cursor/start`, { method: "POST" });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || "Could not start Cursor sign-in.");
  }
  return response.json();
}

export async function signOutCursor(): Promise<void> {
  await apiFetch(`/auth/cursor`, { method: "DELETE" });
}

export type SandboxEnforce = "required" | "preferred" | "off";

export interface SandboxSettings {
  enforce: SandboxEnforce;
  network: boolean;
  filesystem: { readable: string[]; writable: string[]; deny: string[]; grantable: string[] };
  limits: Record<string, number>;
  umask: string | null;
  nice: number;
}

export async function setSandboxEnforce(enforce: SandboxEnforce): Promise<void> {
  await apiFetch(`/settings/sandbox`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sandbox: { enforce } }),
  });
}

export interface McpTool {
  name: string;
  title?: string | null;
  description?: string | null;
  input_schema?: unknown;
}

export interface McpServerTools {
  name: string;
  tools: McpTool[];
  enabled?: boolean;
  // "global" from the home directory or Composio, "workspace" from the folder's own mcp.json.
  scope?: "global" | "workspace";
}

// Skills in the selected folder, home globals included, independent of any agent.
export async function fetchSkills(workingDirectory?: string): Promise<AgentSkill[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  try {
    const data = await cachedDiscovery(discoveryKey("/skills", workingDirectory), () =>
      fetchJson<{ skills?: AgentSkill[] }>(`/skills${query}`),
    );
    return data.skills ?? [];
  } catch (caught) {
    swallowed({ component: "api", operation: "list the skills" }, caught);
    return [];
  }
}

// MCP servers for the selected folder, never the server's launch directory. The pool is a shared union.
export async function fetchMcpTools(workingDirectory?: string): Promise<McpServerTools[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  try {
    const data = await cachedDiscovery(discoveryKey("/mcp/tools", workingDirectory), () =>
      fetchJson<{ servers?: McpServerTools[] }>(`/mcp/tools${query}`),
    );
    return data.servers ?? [];
  } catch (caught) {
    swallowed({ component: "api", operation: "list the MCP tools" }, caught);
    return [];
  }
}

// Subscribe to live server events. One shared stream, since a stream per subscriber exhausts the pool.
const eventListeners = new Set<(event: { type: string }) => void>();
let sharedEventStream: { close: () => void } | null = null;

//: Told whenever the shared stream connects or drops, so the interface can show it.
const connectionListeners = new Set<(connected: boolean) => void>();
let lastReportedConnection: boolean | null = null;

/** Watch whether the daemon is reachable. Answers with an unsubscribe. */
export function subscribeConnection(listener: (connected: boolean) => void): () => void {
  connectionListeners.add(listener);
  if (lastReportedConnection !== null) listener(lastReportedConnection);
  ensureEventStream();
  return () => {
    connectionListeners.delete(listener);
  };
}

function ensureEventStream(): void {
  if (sharedEventStream) return;
  sharedEventStream = openEventStream(
    "/events",
    (raw) => {
      try {
        const event = JSON.parse(raw);
        if (event.type === "agents_changed") invalidateDiscoveryCache();
        eventListeners.forEach((listener) => listener(event));
      } catch {
        // ignore malformed
      }
    },
    (connected) => {
      if (connected === lastReportedConnection) return;
      lastReportedConnection = connected;
      // What comes back may be a different daemon, so forgetting the endpoint is what reaches it.
      if (!connected) forgetDaemonEndpoint();
      connectionListeners.forEach((listener) => listener(connected));
    },
  );
}

export function subscribeEvents(onEvent: (event: { type: string }) => void): () => void {
  eventListeners.add(onEvent);
  ensureEventStream();
  return () => {
    eventListeners.delete(onEvent);
    // Close the shared stream once nothing listens, so it reopens cleanly for the next subscriber.
    if (eventListeners.size === 0 && sharedEventStream) {
      sharedEventStream.close();
      sharedEventStream = null;
    }
  };
}

// The default workspace, with its folder name, so the selector never has to derive one.
export async function fetchHomeDirectory(): Promise<{ path: string; name: string }> {
  const response = await apiFetch(`/home`);
  const data = await response.json();
  return { path: String(data.path ?? ""), name: String(data.name ?? "") };
}

// Best-effort home directory of an SSH host, or "" when it is unknown or unreachable.
export async function fetchHostHomeDirectory(alias: string): Promise<string> {
  try {
    const response = await apiFetch(`/hosts/${encodeURIComponent(alias)}/home`);
    if (!response.ok) return "";
    const data = await response.json();
    return String(data.path ?? "");
  } catch (caught) {
    swallowed({ component: "api", operation: "read a host's home directory" }, caught);
    return "";
  }
}

// A session as the registry knows it. `parent` is its creator, and the token is never listed.
export interface SessionGoal {
  text: string;
  // What the end state is for, which is what lets a closed route be told apart from a lost goal.
  purpose?: string;
  requirements: string[];
  // `active` while worked, `blocked` on an obstacle it cannot pass, `parked` after a long unattended stretch,
  // `satisfied` or `cleared` once it is resolved and kept on the record rather than dropped.
  status: "active" | "blocked" | "parked" | "satisfied" | "cleared";
  blocker: string;
  evidence?: string;
  // The last thing the review told the session to do, which is also what opened its most recent turn.
  direction?: string;
  // True only while the review is actually reading the work, which is a moment rather than a state.
  reviewing?: boolean;
}

export interface SessionSummary {
  id: string;
  agent: string;
  parent: string;
  // Whether the session still exists. Durable: a session is a record, and only its process was transient.
  lifecycle: "live" | "ended";
  // What it is doing now, derived and never stored: working, waiting, idle, asleep or ended.
  activity: "working" | "waiting" | "idle" | "asleep" | "ended";
  // How an ended session finished: `exited` or `failed`. Empty while it is live.
  outcome: string;
  awaiting_input: boolean;
  recording_memory: boolean;
  title: string;
  working_directory: string;
  workspace_id: string;
  permission_mode: PermissionMode;
  created_at: string;
  updated_at: string;
  exit_reason: string;
  goal?: SessionGoal | null;
}

// Live sessions only by default; `all` includes the ones that have exited.
export async function fetchSessions(
  options?: ApiRequestOptions & { all?: boolean },
): Promise<SessionSummary[]> {
  const data = await rpc<{ sessions?: SessionSummary[] }>(
    "session.list",
    options?.all ? { all: true } : {},
    options,
  );
  return data.sessions ?? [];
}

export async function fetchSession(
  sessionId: string,
  options?: ApiRequestOptions,
): Promise<SessionSummary | null> {
  if (!sessionId) return null;
  try {
    const data = await rpc<{ session: SessionSummary }>("session.get", { id: sessionId }, options);
    return data.session ?? null;
  } catch (caught) {
    swallowed({ component: "api", operation: "read a session" }, caught);
    return null;
  }
}

// A session and its descendants, returned flat with each carrying its parent, so a caller nests them.
export interface SessionTree {
  session: SessionSummary;
  descendants: SessionSummary[];
}

export async function sessionTree(
  sessionId: string,
  options?: ApiRequestOptions,
): Promise<SessionTree | null> {
  if (!sessionId) return null;
  try {
    return await rpc<SessionTree>("session.tree", { id: sessionId }, options);
  } catch (caught) {
    swallowed({ component: "api", operation: "read a session tree" }, caught);
    return null;
  }
}

// Create a session: the one place the agent, directory, mode and parent are fixed.
export interface SessionCreateInput {
  agent: string;
  workingDirectory?: string;
  // Where a session runs — in place, on a branch, or in a worktree — chosen once, here.
  worktreeStrategy?: WorktreeStrategy;
  permissionMode?: PermissionMode;
  workspaceId?: string;
  parent?: string;
}

// `parent` and `permissionMode` come back because either may differ from what was asked for.
export interface SessionCreated {
  id: string;
  token: string;
  agent?: string;
  parent?: string;
  permission_mode?: PermissionMode;
}

export async function sessionCreate(
  input: SessionCreateInput,
  options?: ApiRequestOptions,
): Promise<SessionCreated> {
  return rpc<SessionCreated>(
    "session.create",
    {
      agent: input.agent,
      working_directory: input.workingDirectory ?? "",
      worktree_strategy: input.worktreeStrategy ?? "none",
      permission_mode: input.permissionMode ?? "ask",
      workspace_id: input.workspaceId ?? "",
      ...(input.parent ? { parent: input.parent } : {}),
    },
    options,
  );
}

// Change an existing session's policy, reaching the next tool call. The answer is the mode that took.
export async function setSessionPermissionMode(
  sessionId: string,
  mode: PermissionMode,
): Promise<PermissionMode> {
  const data = await rpc<{ permission_mode?: PermissionMode }>("session.permission_mode", {
    id: sessionId,
    permission_mode: mode,
  });
  return data.permission_mode ?? mode;
}

// What became of a message: a send succeeds at HTTP and is still refused by the session, in the body.
export interface SendOutcome {
  /** Whether the session took the message. When false, nothing was delivered and nothing ran. */
  accepted: boolean;
  /** The session is parked on a human decision, which is why it took nothing. */
  awaitingInput: boolean;
  /** What that decision is, as a sentence: "a permission decision for `cat …`". */
  waitingOn: string;
  /** It reached a turn already in flight, at its next safe point, rather than starting one. */
  injected: boolean;
  /** The turn this started, when it started one. */
  taskId: string;
}

// Drive a turn: an idle session starts one, a busy one takes it at the next safe point.
export async function sessionSend(
  sessionId: string,
  parts: A2APart[],
  metadata: Record<string, unknown> = {},
  options?: ApiRequestOptions,
): Promise<SendOutcome> {
  const data = await rpc<{
    accepted?: unknown;
    awaiting_input?: unknown;
    waiting_on?: unknown;
    injected?: unknown;
    task_id?: string;
  }>("session.send", { id: sessionId, parts, metadata }, options);
  return {
    // Absent means accepted: only an explicit `false` is a refusal.
    accepted: data.accepted !== false,
    awaitingInput: data.awaiting_input === true,
    waitingOn: typeof data.waiting_on === "string" ? data.waiting_on : "",
    injected: data.injected === true,
    taskId: data.task_id ?? "",
  };
}

// A turn's parts: prose plus structured payloads, which travel as DataParts so the agent gets JSON.
export function messageParts(text: string, dataParts?: Record<string, unknown>[]): A2APart[] {
  const parts: A2APart[] = [];
  if (text) parts.push({ kind: "text", text });
  // Wrapped in one namespaced key, since that dict is open and reaches a session's own A2A socket.
  for (const dataPart of dataParts ?? [])
    parts.push({ kind: "data", data: wrapPartPayload(dataPart) });
  if (parts.length === 0) parts.push({ kind: "text", text: "" });
  return parts;
}

// One turn from the store, so it answers whether the session that ran it is alive or long since reaped.
export async function turnGet(turnId: string): Promise<A2ATurn | null> {
  if (!turnId) return null;
  try {
    const data = await rpc<{ turn: A2ATurn }>("turn.get", { turn_id: turnId });
    return data.turn ?? null;
  } catch (caught) {
    swallowed({ component: "api", operation: "read a turn" }, caught);
    return null;
  }
}

// Health and counts. The launcher only cares that it answers, so the payload stays untyped.
export async function daemonStatus(
  options?: ApiRequestOptions & { signal?: AbortSignal },
): Promise<Record<string, unknown> | null> {
  try {
    return await rpc<Record<string, unknown>>("daemon.status", {}, options);
  } catch (caught) {
    swallowed({ component: "api", operation: "read the daemon status" }, caught);
    return null;
  }
}

// Every turn a session has taken. Throws on failure, so a transient error is not read as an empty session.
export async function fetchSessionTurns(
  sessionId: string,
  signal?: AbortSignal,
): Promise<A2ATurn[]> {
  const data = await rpc<{ turns?: A2ATurn[] }>("session.history", { id: sessionId }, { signal });
  return data.turns ?? [];
}

export interface SessionTurnsPage {
  turns: A2ATurn[];
  next_before_row_id: number | null;
  has_more: boolean;
}

export async function fetchSessionDraft(sessionId: string): Promise<string> {
  const response = await apiFetch(`/sessions/${sessionId}/draft`);
  if (!response.ok) return "";
  const data = await response.json();
  return String(data.input_draft ?? "");
}

export async function saveSessionDraft(sessionId: string, inputDraft: string): Promise<void> {
  if (!sessionId) return;
  await apiFetch(`/sessions/${sessionId}/draft`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input_draft: inputDraft }),
  });
}

/** One entry in a session's memory: a finding the work established, or an instruction the person gave. */
export interface RecordEntry {
  id: string;
  category?: string;
  claim?: string;
  detail?: string;
  evidence?: string;
  occasion?: string;
  standing?: "verified" | "reported" | "inferred";
  kind?: string;
  summary?: string;
  still_binding?: boolean;
  supersedes?: string[];
  revision?: "correction" | "refinement" | "merge" | "retraction" | "";
  written_at?: string;
}

/** A session's memory, live entries only unless the whole chain is asked for. */
export async function fetchSessionRecord(
  sessionId: string,
  ledger: "observations" | "directives",
  signal?: AbortSignal,
  liveOnly = true,
): Promise<RecordEntry[]> {
  const response = await apiFetch(
    `/sessions/${encodeURIComponent(sessionId)}/record?ledger=${ledger}&live_only=${liveOnly}`,
    { signal },
  );
  if (!response.ok) throw new Error(`record request failed: ${response.status}`);
  const data = (await response.json()) as { entries?: RecordEntry[] };
  return data.entries ?? [];
}

export async function fetchSessionTurnsPage(
  sessionId: string,
  beforeRowId?: number | null,
  signal?: AbortSignal,
  limit = 400,
): Promise<SessionTurnsPage> {
  const data = await rpc<{
    turns?: A2ATurn[];
    next_before_row_id?: number | null;
    has_more?: boolean;
  }>(
    "session.history",
    { id: sessionId, limit, ...(beforeRowId != null ? { before_row_id: beforeRowId } : {}) },
    { signal },
  );
  return {
    turns: data.turns ?? [],
    next_before_row_id: data.next_before_row_id ?? null,
    has_more: !!data.has_more,
  };
}

// The outcome of resolving a prompt, with `status` distinguishing delivered, already-answered and gone.
export interface ResolveResult {
  ok: boolean;
  status: "resolved" | "stale" | "unknown" | "error" | "network";
}

// Answer a gate the session is parked on; the daemon relays it and the worker resumes.
async function sessionRespond(payload: Record<string, unknown>): Promise<ResolveResult> {
  try {
    // The session answers `{resolved: boolean}` — see `worker/server.py`, which is what builds this reply.
    const data = await rpc<{ resolved?: unknown }>("session.respond", payload);
    if (data.resolved === true) return { ok: true, status: "resolved" };
    // `false` is not a failure: the turn moved on, or the gate was answered twice. Settle the card quietly.
    return { ok: true, status: "stale" };
  } catch (error) {
    // A transport failure is worth retrying; a rejected request means it is gone.
    return { ok: false, status: error instanceof TypeError ? "network" : "error" };
  }
}

// The only runtime decisions are per-call allow-once and deny.
export async function resolvePermission(
  sessionId: string,
  requestId: string,
  decision: "deny" | "allow_once",
): Promise<ResolveResult> {
  return sessionRespond({ id: sessionId, request_id: requestId, decision });
}

// Answer a pending ask_user: one entry per question, in order, and `declined` for a dismissed prompt.
export async function resolveQuestion(
  sessionId: string,
  requestId: string,
  answers: unknown[],
  declined = false,
): Promise<ResolveResult> {
  return sessionRespond({ id: sessionId, request_id: requestId, answers, declined });
}

// Whether the cancel reached the session, so a caller can say the turn may still be running.
export async function cancelTurn(sessionId: string): Promise<boolean> {
  try {
    const result = await rpc<{ cancelled?: unknown }>("turn.cancel", { id: sessionId });
    return result?.cancelled !== false;
  } catch (caught) {
    swallowed({ component: "api", operation: "cancel a turn" }, caught);
    return false;
  }
}

// Terminate a session and reap its subtree, since children are sessions of their own.
export async function deleteSession(sessionId: string): Promise<boolean> {
  try {
    await rpc("session.end", { id: sessionId });
    return true;
  } catch (caught) {
    swallowed({ component: "api", operation: "delete a session" }, caught);
    return false;
  }
}

// Call off a session's goal. A turn in flight finishes, so this reports whether there was a goal.
export async function clearSessionGoal(sessionId: string): Promise<boolean> {
  try {
    const result = await rpc<{ cleared?: boolean }>("session.goal_clear", { id: sessionId });
    return Boolean(result?.cleared);
  } catch (caught) {
    swallowed({ component: "api", operation: "call off a session goal" }, caught);
    return false;
  }
}

export async function compactSession(sessionId: string): Promise<boolean> {
  try {
    const result = await rpc<{ compacting?: boolean }>("session.compact", { id: sessionId });
    return Boolean(result?.compacting);
  } catch (caught) {
    swallowed({ component: "api", operation: "compact a session" }, caught);
    return false;
  }
}

// Stopping one call is the session's single cancel, narrowed to the call the user pointed at.
export async function abortToolCall(sessionId: string, toolCallId: string): Promise<boolean> {
  try {
    // `false` means the call was not among those running, so stopping it stopped nothing.
    const result = await rpc<{ cancelled?: unknown }>("turn.cancel", {
      id: sessionId,
      tool_call_id: toolCallId,
    });
    return result?.cancelled !== false;
  } catch (caught) {
    swallowed({ component: "api", operation: "abort a tool call" }, caught);
    return false;
  }
}

// Detach a blocking foreground command so it keeps running and the turn continues.
export async function sendToolToBackground(
  sessionId: string,
  toolCallId: string,
): Promise<boolean> {
  try {
    const result = await rpc<{ backgrounded?: boolean }>("jobs.detach", {
      id: sessionId,
      tool_call_id: toolCallId,
    });
    return Boolean(result?.backgrounded);
  } catch (caught) {
    swallowed({ component: "api", operation: "send a tool call to the background" }, caught);
    return false;
  }
}

export interface BackgroundJob {
  job_id: string;
  kind: string;
  tool_call_id: string;
  arguments: Record<string, unknown>;
  started_at: string;
  detached: boolean;
}

export async function fetchBackgroundJobs(sessionId: string): Promise<BackgroundJob[]> {
  try {
    const result = await rpc<{ jobs?: BackgroundJob[] }>("jobs.list", { id: sessionId });
    return Array.isArray(result?.jobs) ? result.jobs : [];
  } catch (caught) {
    swallowed({ component: "api", operation: "list the background jobs" }, caught);
    return [];
  }
}

export interface DirectoryValidation {
  valid: boolean;
  exists: boolean;
  is_directory: boolean;
  is_absolute: boolean;
  is_git_repository: boolean;
  repository_root: string;
  git_branch: string;
  git_head: string;
  git_short_head: string;
  git_dirty: boolean;
  git_detached: boolean;
  git_label: string;
  git_commit_subject: string;
  git_commit_author: string;
  git_commit_author_email: string;
  git_commit_author_date: string;
  git_upstream: string;
  git_ahead: number;
  git_behind: number;
  git_staged_count: number;
  git_unstaged_count: number;
  git_untracked_count: number;
  git_conflicted_count: number;
  path: string;
}

export async function validateWorkingDirectory(directory: string): Promise<DirectoryValidation> {
  const response = await apiFetch(`/directory/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ directory }),
  });
  return response.json();
}

export function subscribeGitStatus(
  directory: string,
  onStatus: (status: DirectoryValidation) => void,
): () => void {
  const stream = openEventStream(
    `/git/status/stream?directory=${encodeURIComponent(directory)}`,
    (raw) => {
      try {
        onStatus(JSON.parse(raw) as DirectoryValidation);
      } catch {
        // ignore malformed
      }
    },
  );
  return () => stream.close();
}

export async function browseWorkingDirectory(): Promise<{
  path: string;
  cancelled: boolean;
  error?: string;
}> {
  const response = await apiFetch(`/directory/browse`, { method: "POST" });
  return response.json();
}

export async function revealInFinder(path: string): Promise<boolean> {
  try {
    const response = await apiFetch(`/directory/reveal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    return response.ok;
  } catch (caught) {
    swallowed({ component: "api", operation: "reveal a path in Finder" }, caught);
    return false;
  }
}

// Open the browser's remote-debugging settings, when the browser tool reports the switch is off.
export async function openBrowserRemoteDebugging(browserName = "chrome"): Promise<boolean> {
  try {
    const response = await apiFetch(
      `/browser/enable-remote-debugging?browser_name=${encodeURIComponent(browserName)}`,
      { method: "POST" },
    );
    return response.ok;
  } catch (caught) {
    swallowed({ component: "api", operation: "open the browser's remote debugging" }, caught);
    return false;
  }
}

export async function fetchMessageHistory(workingDirectory: string): Promise<string[]> {
  const response = await apiFetch(
    `/messages/history?working_directory=${encodeURIComponent(workingDirectory)}`,
  );
  if (!response.ok) throw new Error(`Failed to fetch message history (${response.status})`);
  const data = await response.json();
  return data.messages as string[];
}

export async function saveMessageHistory(workingDirectory: string, message: string): Promise<void> {
  const response = await apiFetch(`/messages/history`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ working_directory: workingDirectory, message }),
  });
  if (!response.ok) throw new Error(`Failed to save message history (${response.status})`);
}

// A2A protocol types (the subset the client consumes)

export type A2APartKind = "text" | "data" | "file";

export interface A2APart {
  kind: A2APartKind;
  text?: string;
  data?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

// A turn's control-state under one URI-namespaced key, which is A2A's convention for an extension.
export const TURN_STATE_KEY = "urn:langmesh:ext:turn:v1";

// What opened a turn. `peer` is another session speaking, `goal` is the review that keeps one going, and
// neither is the user.
export type TurnKind = "user" | "peer" | "goal" | "autonomous" | "compaction";

export interface TurnState {
  kind?: TurnKind;
  peerSender?: string;
  referenceTurnIds?: string[];
}

// The harness's payload inside a DataPart, or `{}` when the part is not ours.
export function partPayload(data: Record<string, unknown> | undefined): Record<string, unknown> {
  const payload = data?.[TURN_STATE_KEY];
  return payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
}

export function wrapPartPayload(payload: Record<string, unknown>): Record<string, unknown> {
  return { [TURN_STATE_KEY]: payload };
}

export function turnState(turn: { metadata?: Record<string, unknown> } | undefined): TurnState {
  const state = turn?.metadata?.[TURN_STATE_KEY];
  return state && typeof state === "object" ? (state as TurnState) : {};
}

export interface A2AMessage {
  role: "user" | "agent";
  parts: A2APart[];
  messageId?: string;
  contextId?: string;
  turnId?: string;
  referenceTurnIds?: string[];
  metadata?: Record<string, unknown>;
}

export interface A2AArtifact {
  artifactId?: string;
  name?: string;
  parts: A2APart[];
}

export interface A2ATurnStatus {
  state: string;
  message?: A2AMessage;
  timestamp?: string;
}

export interface A2ATurn {
  id: string;
  contextId: string;
  kind?: string;
  status: A2ATurnStatus;
  artifacts?: A2AArtifact[];
  history?: A2AMessage[];
  metadata?: Record<string, unknown>;
}

export function parseSseFrame(frame: string): string {
  return frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n")
    .trim();
}

// Read an SSE body frame by frame. `onFrame` returns "stop" to end the read on an in-band terminator.
async function pumpEventStream(
  body: ReadableStream<Uint8Array>,
  onFrame: (raw: string) => void | "stop",
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const raw = parseSseFrame(frame);
      if (!raw) continue;
      if (onFrame(raw) === "stop") return;
    }
    if (done) break;
  }
  const trailing = parseSseFrame(buffer);
  if (trailing) onFrame(trailing);
}

// A long-lived SSE subscription over `fetch`: EventSource cannot carry the capability token.
function openEventStream(
  path: string,
  onData: (raw: string) => void,
  onHealth?: (connected: boolean) => void,
): { close: () => void } {
  const controller = new AbortController();
  let closed = false;

  // Reconnects on its own and reports whether it is connected, so the interface can say the daemon went.
  const run = async () => {
    while (!closed) {
      try {
        const response = await apiFetch(path, {
          signal: controller.signal,
          headers: { Accept: "text/event-stream" },
        });
        if (response.ok && response.body) {
          onHealth?.(true);
          await pumpEventStream(response.body, onData);
        }
      } catch {
        // A deliberate close and a dropped connection arrive alike; the `closed` flag tells them apart.
      }
      if (closed) return;
      onHealth?.(false);
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  };
  void run();

  return {
    close: () => {
      closed = true;
      controller.abort();
    },
  };
}

// A live view of a session: a snapshot, then a part-granular tail, then `done` when the turn ends.
export type SessionStreamFrame =
  | { kind: "snapshot"; turns: A2ATurn[] }
  // A single part as the session emitted it, so prose arrives as a run rather than an assembled message.
  | { kind: "live"; seq: number; part: A2APart }
  | { kind: "turn"; seq: number; running: boolean }
  | { kind: "done" };

export function attachSession(
  sessionId: string,
  onFrame: (frame: SessionStreamFrame) => void,
  onDone: () => void,
): { abort: () => void } {
  const controller = new AbortController();
  apiFetch(`/sessions/${encodeURIComponent(sessionId)}/attach`, {
    signal: controller.signal,
    headers: { Accept: "text/event-stream" },
  })
    .then(async (response) => {
      if (!response.ok || !response.body) {
        // Silence here reads exactly like a session with nothing to say, so say it.
        swallowed(
          { component: "session-stream", operation: "attach to the session" },
          new Error("the daemon refused the attach stream"),
          { status: response.status, session: sessionId },
        );
        return;
      }
      await pumpEventStream(response.body, (raw) => {
        let frame: SessionStreamFrame;
        try {
          frame = JSON.parse(raw) as SessionStreamFrame;
        } catch {
          // A stream that closes is the normal end of one; nothing to report.
          return;
        }
        onFrame(frame);
        if (frame.kind === "done") {
          controller.abort();
          return "stop";
        }
      });
    })
    .catch(() => {
      // An abort and a dropped connection both end the stream; onDone still fires.
    })
    .finally(onDone);

  return { abort: () => controller.abort() };
}

// The transport for handled-error reports, installed here because this module owns the daemon's address.
setFaultSender((fault) =>
  apiFetch("/telemetry/faults", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fault),
  }),
);
