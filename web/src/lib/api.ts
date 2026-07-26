import type { ArtifactAnnotationRecord, ArtifactImageAnnotation } from "./artifact-annotations";

// Where the daemon lives, and what proves we may talk to it. The CLI and agents reach
// `daisyd` over its unix socket, but a webview has no such transport, so the daemon also
// serves its control plane on a loopback TCP listener for GUI clients, gated by a
// capability token. Both the address and the token are resolved at runtime rather than
// baked in, because the desktop app can point at the local daemon or at a remote one
// reached through an SSH tunnel — and those are *different daemons with different
// tokens*. Resolution order for the address:
//   1. an explicit target set via `setApiBase` (a connection the user activated), then
//   2. the endpoint the Tauri shell reports (`daemon_endpoint`), then
//   3. a build-time default from NEXT_PUBLIC_DAISY_API_BASE, then
//   4. the conventional local daemon address.
// The connection layer (profiles UI / local store) writes the explicit target.
const DEFAULT_API_BASE =
  (typeof process !== "undefined" ? process.env.NEXT_PUBLIC_DAISY_API_BASE : "") || "http://127.0.0.1:8823";
const API_BASE_STORAGE_KEY = "daisy.apiBase";
const API_TOKEN_STORAGE_KEY = "daisy.apiToken";

function runningInTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function readStoredValue(key: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  if (runningInTauri()) return fallback;
  try {
    return window.localStorage.getItem(key) || fallback;
  } catch {
    // localStorage can be unavailable in restricted contexts.
    return fallback;
  }
}

let API_BASE = readStoredValue(API_BASE_STORAGE_KEY, DEFAULT_API_BASE);

// The token for the daemon we are actually talking to. Two sources, and the distinction
// matters: the *local* token is the one the Tauri shell reads out of the runtime
// directory of this machine, and it authenticates nothing on a remote host. A connection
// profile therefore carries its own token, and when one is activated it wins — otherwise
// every SSH-tunnelled daemon would be handed the local machine's secret and answer 401.
let localDaemonToken = "";
// Null, not "": a profile deliberately saved without a token must present none rather
// than silently fall back to the local daemon's.
let activeConnectionToken: string | null = readStoredValue(API_TOKEN_STORAGE_KEY, "") || null;
// True once a connection has been activated, so the shell's local endpoint no longer
// overwrites the target the user chose.
let apiBaseWasChosen = Boolean(readStoredValue(API_BASE_STORAGE_KEY, ""));
let daemonEndpointPromise: Promise<void> | null = null;

async function resolveDaemonEndpoint(): Promise<void> {
  if (!runningInTauri()) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    const endpoint = await invoke<{ url: string; token: string }>("daemon_endpoint");
    if (endpoint?.url && !apiBaseWasChosen) API_BASE = endpoint.url.replace(/\/+$/, "");
    localDaemonToken = endpoint?.token ?? "";
  } catch {
    // The shell could not report an endpoint (the daemon is not up yet). Leave the
    // defaults in place; the request that follows fails and the launcher surfaces it.
  }
}

// Resolved once and memoized: the endpoint is fixed for the life of the daemon the
// shell started, and every request would otherwise pay an IPC round trip.
function ensureDaemonEndpoint(): Promise<void> {
  if (!daemonEndpointPromise) daemonEndpointPromise = resolveDaemonEndpoint();
  return daemonEndpointPromise;
}

export interface ApiRequestOptions {
  apiBase?: string;
  // The token to present, when the request is aimed at a daemon other than the active
  // one — a health probe against a saved profile before it has been activated.
  token?: string;
}

function apiBase(options?: ApiRequestOptions): string {
  return (options?.apiBase || API_BASE).replace(/\/+$/, "");
}

function apiUrl(path: string, options?: ApiRequestOptions): string {
  return `${apiBase(options)}${path}`;
}

function requestToken(options?: ApiRequestOptions): string {
  return options?.token ?? activeConnectionToken ?? localDaemonToken;
}

// The token as a query parameter, for the transports that cannot carry a header: a
// WebSocket handshake, and any URL the browser itself loads (an iframe's src, an <img>,
// a download). Everything else goes through `apiFetch` and gets the header.
function withDaemonToken(url: string, options?: ApiRequestOptions): string {
  const token = requestToken(options);
  if (!token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

function websocketUrl(path: string, options?: ApiRequestOptions): string {
  const base = apiBase(options);
  const url = new URL(path, `${base}/`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return withDaemonToken(url.toString(), options);
}

// The single door every request goes through, so the capability token is attached in
// exactly one place and cannot be forgotten at a call site.
async function apiFetch(path: string, options: RequestInit & ApiRequestOptions = {}): Promise<Response> {
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

// The daemon's control plane: one POST carrying `{method, params}`. Lifecycle and reads
// are answered by the daemon itself; a data-plane command (`session.send`, `.cancel`,
// `.respond`) is relayed by the daemon to the owning session's socket, because a webview
// cannot reach that socket. The CLI talks to the socket directly instead — same API,
// different transport.
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
  const payload = await response.json().catch(() => ({})) as { result?: T; error?: { code?: string; message?: string } };
  // The daemon answers with `{result}` or `{error}`; its message names what actually went
  // wrong (no such session, session not running), which is worth more than the status code.
  if (!response.ok || payload.error) {
    throw new Error(payload.error?.message || `${method} failed (${response.status})`);
  }
  return payload.result as T;
}

// The address the client is currently talking to.
export function getApiBase(): string {
  return API_BASE;
}

export function terminalWebSocketUrl(options: { sessionId?: string | null; workingDirectory?: string; terminalKey?: string; locationKind?: string; locationBaseDirectory?: string; locationHostAlias?: string; rows?: number; columns?: number } = {}): string {
  const params = new URLSearchParams();
  if (options.sessionId) params.set("session_id", options.sessionId);
  if (options.workingDirectory) params.set("working_directory", options.workingDirectory);
  if (options.terminalKey) params.set("terminal_key", options.terminalKey);
  if (options.locationKind) params.set("location_kind", options.locationKind);
  if (options.locationBaseDirectory) params.set("location_base_directory", options.locationBaseDirectory);
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

function terminalContextQuery(sessionId: string | null | undefined, workingDirectory: string | undefined): string {
  const params = new URLSearchParams();
  if (sessionId) params.set("session_id", sessionId);
  if (workingDirectory) params.set("working_directory", workingDirectory);
  const query = params.toString();
  return query ? `?${query}` : "";
}

export async function listTerminals(sessionId: string | null, workingDirectory: string): Promise<TerminalInfo[]> {
  const response = await apiFetch(`/terminals${terminalContextQuery(sessionId, workingDirectory)}`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.terminals)
    ? (data.terminals as Array<{ terminal_key: string; cwd?: string; running?: boolean }>).map((entry) => ({
        terminalKey: entry.terminal_key,
        cwd: entry.cwd ?? "",
        running: Boolean(entry.running),
      }))
    : [];
}

export async function deleteTerminal(sessionId: string | null, workingDirectory: string, terminalKey: string): Promise<void> {
  if (!terminalKey) return;
  await apiFetch(`/terminals/${encodeURIComponent(terminalKey)}${terminalContextQuery(sessionId, workingDirectory)}`, {
    method: "DELETE",
  });
}

// Point the client at a daemon, with the token that authorises talking to *that* one.
// Persists the choice so it survives reloads. Callers typically reload the app
// afterwards so in-flight streams and caches restart cleanly against the new backend.
//
// The token is required rather than optional so a call site cannot quietly leave the
// previous daemon's token in place: pass "" for the local daemon, whose token the Tauri
// shell reads from the runtime directory instead.
export function setApiBase(url: string, token: string): void {
  const normalized = url.trim().replace(/\/+$/, "");
  API_BASE = normalized || DEFAULT_API_BASE;
  apiBaseWasChosen = Boolean(normalized);
  activeConnectionToken = token.trim() || null;
  if (typeof window === "undefined") return;
  if (runningInTauri()) return;
  try {
    if (normalized) {
      window.localStorage.setItem(API_BASE_STORAGE_KEY, normalized);
    } else {
      window.localStorage.removeItem(API_BASE_STORAGE_KEY);
    }
    if (activeConnectionToken) {
      window.localStorage.setItem(API_TOKEN_STORAGE_KEY, activeConnectionToken);
    } else {
      window.localStorage.removeItem(API_TOKEN_STORAGE_KEY);
    }
  } catch {
    // Best-effort persistence; the in-memory value still applies this session.
  }
}

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

// The URL that serves a file (and its sibling assets) for an `open_artifact` artifact —
// the backend `/artifact-page/<abs path>` route reads the file and, for HTML, injects the
// artifact runtime. Each path segment is encoded but the slashes are kept, so relative
// assets inside a rendered page still resolve.
//
// When the file lives on a REMOTE location, its session + location ride in a sentinel
// first path segment (`@ctx=<base64url>`) so the backend reads through that location's
// executor — and, crucially, so a page's relative assets (which drop a query string but
// keep the path prefix) inherit the same remote context. Local files omit it.
export function artifactPageUrl(path: string, context?: { location?: string; session?: string }): string {
  const normalized = path.replace(/^\/+/, "");
  if (!normalized) return "";
  const encoded = normalized.split("/").map(encodeURIComponent).join("/");
  const location = context?.location ?? "";
  // Only a genuinely remote location needs executor-backed serving; a local file is read
  // straight off disk (the fast FileResponse path, with range support for PDFs).
  if (location.startsWith("ssh://")) {
    const payload = JSON.stringify({ s: context?.session ?? "", l: location });
    const token = btoa(payload).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    return withDaemonToken(`${API_BASE}/artifact-page/@ctx=${token}/${encoded}`);
  }
  return withDaemonToken(`${API_BASE}/artifact-page/${encoded}`);
}

// The URL that renders an external page. It is fetched and re-served from the
// backend `/artifact-proxy` route with anti-framing headers (X-Frame-Options /
// CSP frame-ancestors) stripped — otherwise sites that refuse to be framed (the
// BBC, most news sites) render as a blank, blocked frame.
export function artifactProxyUrl(url: string): string {
  if (!url) return "";
  return withDaemonToken(`${API_BASE}/artifact-proxy?url=${encodeURIComponent(url)}`);
}

// A generic uploaded file. Feature-agnostic: the core knows only the stored file and
// its metadata, plus any image annotations the user has drawn (carried along to the
// model when the message is sent). Skills layer their own meaning on top separately.
export interface Attachment {
  upload_id: string;
  title: string;
  filename: string;
  path: string;
  mime_type: string;
  size: number;
  sha256: string;
  annotations?: ArtifactImageAnnotation[];
}

export async function uploadFile(file: File): Promise<Attachment> {
  const body = new FormData();
  body.append("file", file);
  const response = await apiFetch(`/uploads`, {
    method: "POST",
    body,
  });
  if (!response.ok) throw new Error(`Failed to upload ${file.name} (${response.status})`);
  return await response.json() as Attachment;
}

// Register a user attachment that lives on the server's own filesystem *by path*, with
// no copy — the desktop app hands over the real OS path. Only valid when the server and
// the file are the same machine (a local connection); a remote-server connection must
// upload the bytes with uploadFile instead, since a local path is meaningless there.
export async function referenceAttachment(path: string): Promise<Attachment> {
  const response = await apiFetch(`/attachments/reference`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) throw new Error(`Failed to attach ${path} (${response.status})`);
  return await response.json() as Attachment;
}

// Projects, locations, and the SSH host registry.

// A connectable SSH host from ~/.ssh/config (the source of truth for remotes).
export interface SshHost {
  alias: string;
  hostname: string;
  user: string;
  port: number;
  identity_files: string[];
}

// A named place a project runs tools in. `name` is derived from the connection (host
// alias / folder), not user-entered. `permission_mode` is the one execution policy a
// location carries. `uri` is the fully-qualified identifier the agent addresses.
export interface Location {
  id: string;
  project_id: string;
  name: string;
  kind: "local" | "remote";
  host_alias: string;
  host_known: boolean;
  base_directory: string;
  uri: string;
  permission_mode: string;
  created_at: string;
}

export interface Project {
  id: string;
  created_at: string;
  updated_at: string;
  session_count: number;
  locations?: Location[];
}

// The editable shape of a location (create/update). No `name` — the server derives it.
export interface LocationInput {
  kind: "local" | "remote";
  base_directory: string;
  host_alias?: string;
  permission_mode?: string;
}

export interface ProjectCreateInput {
  locations: LocationInput[];
}

export async function listSshHosts(): Promise<SshHost[]> {
  const response = await apiFetch(`/hosts`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.hosts) ? (data.hosts as SshHost[]) : [];
}

export async function listProjects(): Promise<Project[]> {
  const response = await apiFetch(`/projects`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.projects) ? (data.projects as Project[]) : [];
}

export async function getProject(projectId: string): Promise<Project | null> {
  const response = await apiFetch(`/projects/${encodeURIComponent(projectId)}`);
  if (!response.ok) return null;
  return await response.json() as Project;
}

export async function createProject(input: ProjectCreateInput): Promise<Project> {
  const response = await apiFetch(`/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to create project (${response.status})`);
  return await response.json() as Project;
}

export async function deleteProject(projectId: string): Promise<void> {
  await apiFetch(`/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
}

export async function createLocation(projectId: string, input: LocationInput): Promise<Location> {
  const response = await apiFetch(`/projects/${encodeURIComponent(projectId)}/locations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to add location (${response.status})`);
  return await response.json() as Location;
}

export async function updateLocation(locationId: string, input: LocationInput): Promise<Location> {
  const response = await apiFetch(`/locations/${encodeURIComponent(locationId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to update location (${response.status})`);
  return await response.json() as Location;
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
  health: string;   // unresolved | ok | unreachable | untrusted
  error: string;
  resolvedName: string;
  resolvedDescription: string;
  skills: string[];
}

export interface RemoteAgentAuthInput {
  type: string;   // none | bearer | api_key | oauth2
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

// Metadata key understood by a session's A2A surface.
// A2A convention: an extension places its attributes under one URI-namespaced key in
// the message `metadata` map, not as bare top-level keys. Mirrors DAISY_METADATA_KEY
// / Metadata in the backend's protocol layer.
export const DAISY_METADATA_KEY = "urn:daisy:ext:turn:v1";
export const CONTENT_BLOCK_METADATA_KEY = "urn:daisy:ext:content-block:v1";

export type PermissionMode = "default" | "auto" | "read_only";
export type WorkspaceStrategy = "none" | "branch" | "worktree";

export interface AgentSummary {
  id: string;
  name: string;
  title?: string;
  // What the agent is for — shown as the subtitle in the agent picker.
  description?: string;
  // The agent's resolved `provider/model` identifier. Empty means the agent is
  // missing a runnable model configuration.
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
  permission_mode: PermissionMode;
  tools_enabled: string[];
  bash: AgentBashConfiguration;
  path: string;
}

export interface SaveAgentConfigurationPayload {
  model?: string;
  provider?: string;
  reasoning_effort?: string;
  permission_mode?: PermissionMode;
  tools_enabled?: string[];
  bash?: Partial<AgentBashConfiguration>;
}

// Agents are scoped to the selected folder: the bundled (server-shipped)
// profiles are always present as a base, then home globals, then that folder's
// own `.agents/agents` (deduped), so passing `workingDirectory` is what makes
// the list track the chosen folder rather than the server's launch directory.
export async function fetchAgents(workingDirectory?: string): Promise<AgentSummary[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const data = await cachedDiscovery(discoveryKey("/agents", workingDirectory), () =>
    fetchJson<{ agents: AgentSummary[] }>(`/agents${query}`)
  );
  return data.agents;
}

export async function fetchAgentConfiguration(agent: string, workingDirectory?: string): Promise<AgentConfiguration> {
  const query = workingDirectory ? `?working_directory=${encodeURIComponent(workingDirectory)}` : "";
  return fetchJson<AgentConfiguration>(`/agents/${encodeURIComponent(agent)}/configuration${query}`);
}

export async function saveAgentConfiguration(agent: string, payload: SaveAgentConfigurationPayload, workingDirectory?: string): Promise<AgentConfiguration> {
  const query = workingDirectory ? `?working_directory=${encodeURIComponent(workingDirectory)}` : "";
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
  // "global" (from ~/.agents) or "project" (from the selected folder's .agents).
  scope?: "global" | "project";
}

export interface AgentCard {
  name: string;
  title?: string;
  description?: string;
  url: string;
  version?: string;
  skills: AgentSkill[];
}

// Discovery: every served agent's A2A AgentCard (with its skills). Skills are
// scoped to the selected project path — the home globals plus that folder's own
// `.agents` skills, deduped — so passing `workingDirectory` is what makes the
// listed skills match the chosen folder rather than the server's launch directory.
export async function fetchAgentCards(workingDirectory?: string): Promise<AgentCard[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const data = await cachedDiscovery(discoveryKey("/agents/cards", workingDirectory), () =>
    fetchJson<{ cards?: AgentCard[] }>(`/agents/cards${query}`)
  );
  return data.cards ?? [];
}

export interface ProviderCredential {
  api_key: string;
  base_url: string;
}

export interface CompactionSettings {
  // Automatic Observational-Memory compaction on/off (manual always works).
  auto: boolean;
  observer_context_fraction: number;
  reflector_observation_fraction: number;
  keep_recent_turns: number;
}

export interface Settings {
  permission_mode: PermissionMode;
  exa_api_key: string;
  composio_api_key: string;
  // Web-fetch engines for the fetch_url tool: Jina Reader (default, optional key)
  // and Firecrawl (optional fallback). Both empty is valid — Jina runs keyless.
  jina_api_key: string;
  firecrawl_api_key: string;
  // Optional proxy for the web-fetch direct tier and file downloads (IP-blocked sites).
  web_fetch_proxy_url: string;
  // What a session's tool children may do, enforced by the operating system. The paths and
  // limits live in the configuration file, where a person edits them the way they would edit
  // any other Unix policy; what the app surfaces is whether it is enforced at all.
  sandbox: SandboxSettings;
  // What this machine can actually enforce with. Empty `backend` means the toggle cannot be
  // honoured here, which the UI has to say rather than imply protection that is absent.
  sandbox_backend: { backend: string; detail: string };
  // Opt-in: inject a snapshot of the user's machine habits (frequent folders, recent
  // files, installed/running apps, most-visited sites) into the system prompt. Off by default.
  user_context_enabled: boolean;
  // Opt-in: let the agent control macOS apps via the computer-use tool. Off by default.
  computer_control_enabled: boolean;
  workspace_strategy: "none" | "branch" | "worktree";
  compaction: CompactionSettings;
  providers: Record<string, ProviderCredential>;
}

// Only reached when the settings request fails outright. `required` matches the harness's own
// default, so a client that cannot read the settings never implies less protection than there is.
const DEFAULT_SANDBOX: SandboxSettings = {
  enforce: "required",
  network: true,
  filesystem: { readable: [], writable: [], deny: [] },
  limits: {},
  umask: null,
  nice: 0,
};

const DEFAULT_COMPACTION: CompactionSettings = {
  auto: false,
  observer_context_fraction: 0.6,
  reflector_observation_fraction: 0.3,
  keep_recent_turns: 6,
};

// Persist the Observational-Memory compaction settings (auto on/off + thresholds).
export async function updateCompactionSettings(changes: Partial<CompactionSettings>): Promise<void> {
  await apiFetch(`/settings/compaction`, {
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

// Toggle the opt-in computer-use tool that controls macOS apps (rebuilds runtimes).
export async function updateComputerControlSetting(enabled: boolean): Promise<void> {
  await apiFetch(`/settings/computer-control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// Whether the server can read Full-Disk-Access-protected data (Screen Time, Safari history) —
// gates the deepest user-context signals. False on any error (e.g. non-macOS).
export async function fetchFullDiskAccess(): Promise<boolean> {
  try {
    const response = await apiFetch(`/system/full-disk-access`);
    if (!response.ok) return false;
    return (await response.json()).granted === true;
  } catch {
    return false;
  }
}

// Open System Settings to the Full Disk Access pane so the user can add Daisy in one hop.
export async function openFullDiskAccessSettings(): Promise<void> {
  await apiFetch(`/system/full-disk-access/open`, { method: "POST" }).catch(() => {});
}

// Whether the app can control other apps (read the accessibility tree, synthesize input) —
// the permission the computer-use tool needs. False on any error (e.g. non-macOS).
export async function fetchAccessibility(): Promise<boolean> {
  try {
    const response = await apiFetch(`/system/accessibility`);
    if (!response.ok) return false;
    return (await response.json()).granted === true;
  } catch {
    return false;
  }
}

// Trigger the system Accessibility prompt and open its pane so the user can grant Daisy.
export async function openAccessibilitySettings(): Promise<void> {
  await apiFetch(`/system/accessibility/open`, { method: "POST" }).catch(() => {});
}

// Quit and relaunch the desktop app (Tauri command). macOS only reflects a new Accessibility
// grant to the bundled server on a fresh launch, so the grant flow offers a one-click restart.
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
  // Capabilities from models.dev (raw snake_case as the /models endpoint sends
  // them). `attachment` gates the composer's file-attach button; `vision` (image
  // input) and `input_modalities` annotate the picker.
  attachment?: boolean;
  vision?: boolean;
  input_modalities?: string[];
  // ISO release date (YYYY-MM-DD) from models.dev, "" if unknown. The picker
  // orders newest-first on this rather than alphabetically.
  release_date?: string;
}

export interface ProviderOption {
  id: string;
  name: string;
  openai_compatible: boolean;
}

export interface ModelsResponse {
  models: ModelOption[];
  providers: ProviderOption[];
}

// API credentials stored in the daemon's configuration.yaml (under $XDG_CONFIG_HOME/daisy).
export async function fetchSettings(): Promise<Settings> {
  const response = await apiFetch(`/settings`);
  if (!response.ok) {
    return { permission_mode: "default", exa_api_key: "", composio_api_key: "", jina_api_key: "", firecrawl_api_key: "", web_fetch_proxy_url: "", sandbox: DEFAULT_SANDBOX, sandbox_backend: { backend: "", detail: "" }, user_context_enabled: false, computer_control_enabled: false, workspace_strategy: "none", compaction: DEFAULT_COMPACTION, providers: {} };
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
  workspace_strategy?: "none" | "branch" | "worktree";
}

export async function saveSettings(settings: SaveSettingsPayload): Promise<void> {
  await apiFetch(`/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

// The model catalog for the picker (with per-model availability) and the provider
// registry.
export async function fetchModels(): Promise<ModelsResponse> {
  const response = await apiFetch(`/models`);
  if (!response.ok) return { models: [], providers: [] };
  return response.json();
}

// Recently selected models (newest first), mirroring the project history — so the
// picker can surface the models a user actually switches between at the top.
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

// ChatGPT-subscription sign-in state for the `chatgpt` provider.
// This is an OAuth session, not a stored key — the token lives server-side and
// this only reports whether one is present and for which account.
// One rate-limit window the ChatGPT/Codex backend enforces (a rolling 5h window
// and a weekly window). `window_minutes` is the source of truth for labeling —
// the 5h/weekly split isn't pinned to a fixed slot across accounts.
export interface ChatGPTUsageWindow {
  key: string;
  used_percent: number;
  window_minutes: number;
  resets_at: number | null;
}

// The account's usage snapshot, captured from `x-codex-*` headers on the last turn.
// Absent (null) until the first turn runs after sign-in — the headers only ride on
// the responses call, so there is no cheaper source to poll.
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

// Begin sign-in: the server binds its loopback callback and returns the OpenAI
// authorize URL to open in a browser. Completion arrives via a `settings_changed`
// broadcast (or by re-polling fetchChatGPTAuthStatus).
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

export async function fetchArtifactAnnotations(contextId: string): Promise<ArtifactAnnotationRecord[]> {
  if (!contextId) return [];
  const response = await apiFetch(`/sessions/${encodeURIComponent(contextId)}/artifact-annotations`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.records) ? data.records as ArtifactAnnotationRecord[] : [];
}

export async function saveArtifactAnnotations(contextId: string, record: ArtifactAnnotationRecord): Promise<void> {
  if (!contextId) return;
  await apiFetch(`/sessions/${encodeURIComponent(contextId)}/artifact-annotations`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      surface_id: record.image.artifactId,
      version_id: record.image.versionId,
      annotations: record.annotations,
      updated_at: record.updatedAt,
    }),
  });
}

export async function deleteArtifactAnnotations(contextId: string, surfaceId: string, versionId: string): Promise<void> {
  if (!contextId || !surfaceId || !versionId) return;
  const query = `surface_id=${encodeURIComponent(surfaceId)}&version_id=${encodeURIComponent(versionId)}`;
  await apiFetch(`/sessions/${encodeURIComponent(contextId)}/artifact-annotations?${query}`, {
    method: "DELETE",
  });
}

// The change kind a version records against its predecessor: added, modified, or deleted.
export type ArtifactChangeType = "A" | "M" | "D";

// A version's byte source is a git blob (a file version) identified by
// `(locationUri, gitDirectory, blobSha)`, streamed from the backend. `download`
// sets an attachment Content-Disposition so the browser saves rather than renders.
export function artifactBytesUrl(options: {
  location: string;
  gitDirectory: string;
  sha: string;
  session: string;
  download?: string;
}): string {
  const params = new URLSearchParams();
  params.set("location", options.location);
  params.set("git_directory", options.gitDirectory);
  params.set("sha", options.sha);
  params.set("session", options.session);
  if (options.download) params.set("download", options.download);
  return withDaemonToken(`${API_BASE}/artifact-bytes?${params.toString()}`);
}

// The whole artifact catalog for a session: the file-history index (every changed
// file, for History mode) plus the surfaces the agent explicitly opened as tabs.
// `scope` = "session" limits to this session's writes; "full" spans all sessions.
export interface ArtifactIndexEntry {
  gitDirectory: string;
  relativePath: string;
  absolutePath: string;
  locationUri: string;
  workTree: string;
  versionCount: number;
  latestCommit: string;
  latestBlob: string;
  latestChange: ArtifactChangeType;
  size: number;
  isPlaceholder: boolean;
  updatedAt: string;
  surfaced: boolean;
  kind: "image" | "html" | "file";
  artifactId: string;
  title: string;
}

export interface ArtifactSurface {
  artifactId: string;
  kind: "image" | "html" | "iframe" | "file";
  title: string;
  source: string;
  gitDirectory: string;
  workTree: string;
  relativePath: string;
  absolutePath: string;
  locationUri: string;
  latestCommit: string;
  latestBlob: string;
  toolCallId: string;
  createdAt: string;
  updatedAt: string;
}

export interface ArtifactVersion {
  versionId: string;
  commitSha: string;
  blobSha: string;
  sequence: number;
  changeType: ArtifactChangeType;
  size: number;
  isPlaceholder: boolean;
  createdAt: string;
  message: string;
  toolCallId: string;
  gitDirectory: string;
  relativePath: string;
  locationUri: string;
  workTree: string;
  annotationCount: number;
}

export type ArtifactScope = "session" | "full";

export async function fetchArtifacts(
  contextId: string,
  scope: ArtifactScope = "session",
): Promise<{ artifacts: ArtifactIndexEntry[]; surfaces: ArtifactSurface[] }> {
  if (!contextId) return { artifacts: [], surfaces: [] };
  const response = await apiFetch(`/sessions/${encodeURIComponent(contextId)}/artifacts?scope=${scope}`);
  if (!response.ok) return { artifacts: [], surfaces: [] };
  const data = await response.json();
  return {
    artifacts: Array.isArray(data.artifacts) ? (data.artifacts as ArtifactIndexEntry[]) : [],
    surfaces: Array.isArray(data.surfaces) ? (data.surfaces as ArtifactSurface[]) : [],
  };
}

export async function fetchArtifactVersions(
  contextId: string,
  gitDirectory: string,
  relativePath: string,
  scope: ArtifactScope = "session",
): Promise<ArtifactVersion[]> {
  if (!contextId || !relativePath) return [];
  const params = new URLSearchParams({ git_directory: gitDirectory, relative_path: relativePath, scope });
  const response = await apiFetch(`/sessions/${encodeURIComponent(contextId)}/artifacts/versions?${params.toString()}`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.versions) ? (data.versions as ArtifactVersion[]) : [];
}

export async function fetchArtifactDiff(
  contextId: string,
  options: { gitDirectory: string; relativePath: string; fromCommit: string; toCommit: string; location: string },
): Promise<string> {
  if (!contextId) return "";
  const params = new URLSearchParams({
    git_directory: options.gitDirectory,
    relative_path: options.relativePath,
    from_commit: options.fromCommit,
    to_commit: options.toCommit,
    location: options.location,
  });
  const response = await apiFetch(`/sessions/${encodeURIComponent(contextId)}/artifacts/diff?${params.toString()}`);
  if (!response.ok) return "";
  const data = await response.json();
  return String(data.diff ?? "");
}

export async function restoreArtifact(
  contextId: string,
  options: { locationUri: string; gitDirectory: string; workTree: string; relativePath: string; commitSha: string },
): Promise<boolean> {
  if (!contextId) return false;
  const response = await apiFetch(`/sessions/${encodeURIComponent(contextId)}/artifacts/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      location_uri: options.locationUri,
      git_directory: options.gitDirectory,
      work_tree: options.workTree,
      relative_path: options.relativePath,
      commit_sha: options.commitSha,
    }),
  });
  return response.ok;
}

export type SandboxEnforce = "required" | "preferred" | "off";

export interface SandboxSettings {
  enforce: SandboxEnforce;
  network: boolean;
  filesystem: { readable: string[]; writable: string[]; deny: string[] };
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
  // "global" (from ~/.agents or the Composio integration) or "project" (from the
  // selected folder's own mcp.json).
  scope?: "global" | "project";
}

// Discovery: tools exposed by each configured MCP server, for the capabilities panel.
// Skills available in the selected folder — home globals plus that folder's own
// `.agents/skills`, deduped — independent of any agent, so the panel can show a
// folder's skills even when it has no agents.
export async function fetchSkills(workingDirectory?: string): Promise<AgentSkill[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  try {
    const data = await cachedDiscovery(discoveryKey("/skills", workingDirectory), () =>
      fetchJson<{ skills?: AgentSkill[] }>(`/skills${query}`)
    );
    return data.skills ?? [];
  } catch {
    return [];
  }
}

// MCP servers are listed for the selected folder: its own `mcp.json` plus the
// home globals and the global Composio integration (deduped), never the server's
// launch directory. The subprocess pool is shared and grows as a union.
export async function fetchMcpTools(workingDirectory?: string): Promise<McpServerTools[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  try {
    const data = await cachedDiscovery(discoveryKey("/mcp/tools", workingDirectory), () =>
      fetchJson<{ servers?: McpServerTools[] }>(`/mcp/tools${query}`)
    );
    return data.servers ?? [];
  } catch {
    return [];
  }
}

// Subscribe to live server events (e.g. agents changed). Returns an unsubscribe.
//
// All subscribers share ONE stream. Opening one per subscriber (many components
// subscribe) quickly exhausted the browser's ~6-connections-per-host limit with
// long-lived SSE streams, starving every other request to the server — most visibly, a
// user-triggered POST could never get a connection and hung forever. A single stream
// fans out to all local listeners instead.
const eventListeners = new Set<(event: { type: string }) => void>();
let sharedEventStream: { close: () => void } | null = null;

function ensureEventStream(): void {
  if (sharedEventStream) return;
  sharedEventStream = openEventStream("/events", (raw) => {
    try {
      const event = JSON.parse(raw);
      if (event.type === "agents_changed") invalidateDiscoveryCache();
      eventListeners.forEach((listener) => listener(event));
    } catch {
      // ignore malformed
    }
  });
}

export function subscribeEvents(onEvent: (event: { type: string }) => void): () => void {
  eventListeners.add(onEvent);
  ensureEventStream();
  return () => {
    eventListeners.delete(onEvent);
    // Close the shared stream once nothing is listening, so it reopens cleanly
    // (and against a possibly-changed API base) when a subscriber returns.
    if (eventListeners.size === 0 && sharedEventStream) {
      sharedEventStream.close();
      sharedEventStream = null;
    }
  };
}

// The default project shown before the user picks anything — the server provides
// its folder name so the selector never has to derive one.
export async function fetchHomeDirectory(): Promise<{ path: string; name: string }> {
  const response = await apiFetch(`/home`);
  const data = await response.json();
  return { path: String(data.path ?? ""), name: String(data.name ?? "") };
}

// Best-effort home directory of an SSH host (for prefilling a location's base directory).
// Returns "" if the host is unknown/unreachable — the field then stays empty for manual entry.
export async function fetchHostHomeDirectory(alias: string): Promise<string> {
  try {
    const response = await apiFetch(`/hosts/${encodeURIComponent(alias)}/home`);
    if (!response.ok) return "";
    const data = await response.json();
    return String(data.path ?? "");
  } catch {
    return "";
  }
}

// A session as the daemon's registry knows it. `parent` is the session that created this
// one — a peer is an ordinary session, so the hierarchy belongs in the
// sidebar rather than in a separate agents panel. The capability token is never listed:
// it is handed to the creator once, at `create`.
export interface SessionSummary {
  id: string;
  agent: string;
  parent: string;
  // starting | running | exited — the process's own lifecycle, not the turn's.
  status: string;
  // Whether a turn is actually in flight. A session's process stays alive between
  // messages, so `status: "running"` means "there is a process", not "it is working".
  busy: boolean;
  awaiting_input: boolean;
  title: string;
  working_directory: string;
  project_id: string;
  permission_mode: PermissionMode;
  pid: number;
  created_at: string;
  updated_at: string;
  exit_reason: string;
}

// Live sessions only by default; `all` includes the ones that have exited.
export async function fetchSessions(options?: ApiRequestOptions & { all?: boolean }): Promise<SessionSummary[]> {
  const data = await rpc<{ sessions?: SessionSummary[] }>("session.list", options?.all ? { all: true } : {}, options);
  return data.sessions ?? [];
}

export async function fetchSession(sessionId: string, options?: ApiRequestOptions): Promise<SessionSummary | null> {
  if (!sessionId) return null;
  try {
    const data = await rpc<{ session: SessionSummary }>("session.get", { id: sessionId }, options);
    return data.session ?? null;
  } catch {
    return null;
  }
}

// A session and everything created beneath it. The daemon returns the descendants flat,
// each carrying its `parent`, so a caller nests them itself rather than being handed a
// shape it would have to flatten to search.
export interface SessionTree {
  session: SessionSummary;
  descendants: SessionSummary[];
}

export async function sessionTree(sessionId: string, options?: ApiRequestOptions): Promise<SessionTree | null> {
  if (!sessionId) return null;
  try {
    return await rpc<SessionTree>("session.tree", { id: sessionId }, options);
  } catch {
    return null;
  }
}

// Create a session: one OS process running one agent, empty until it is sent a
// message. This is the single point where the agent, the directory, the permission
// mode and the parent are fixed — `send` never changes any of them. The returned token
// is the session's capability handle; the daemon relays on our behalf, so the GUI keeps
// it only to identify the session it owns.
export interface SessionCreateInput {
  agent: string;
  workingDirectory?: string;
  // The workspace a session runs in (in place, on a branch, or in a worktree) is chosen
  // once here, like every other piece of its configuration.
  workspaceStrategy?: WorkspaceStrategy;
  permissionMode?: PermissionMode;
  projectId?: string;
  parent?: string;
}

// `parent` and `permissionMode` come back because either may differ from what was asked
// for: a caller identified by its own token becomes the parent whatever it passed, and the
// mode is clamped against that parent. A creator that cannot see the clamp cannot reason
// about what it just made.
export interface SessionCreated {
  id: string;
  token: string;
  agent?: string;
  parent?: string;
  permission_mode?: PermissionMode;
}

export async function sessionCreate(input: SessionCreateInput, options?: ApiRequestOptions): Promise<SessionCreated> {
  return rpc<SessionCreated>("session.create", {
    agent: input.agent,
    working_directory: input.workingDirectory ?? "",
    workspace_strategy: input.workspaceStrategy ?? "none",
    permission_mode: input.permissionMode ?? "default",
    project_id: input.projectId ?? "",
    ...(input.parent ? { parent: input.parent } : {}),
  }, options);
}

// Drive a turn. A message to an idle session starts one; a message to a session that is
// mid-turn is safe-point injected at the next tool boundary — which is why steering is
// no longer a separate call. The response arrives on the attach stream, not here.
export async function sessionSend(
  sessionId: string,
  parts: A2APart[],
  metadata: Record<string, unknown> = {},
  options?: ApiRequestOptions,
): Promise<string> {
  const data = await rpc<{ task_id?: string }>("session.send", { id: sessionId, parts, metadata }, options);
  return data.task_id ?? "";
}

// The parts a turn carries: the typed prose plus any structured payloads (attachments,
// artifact interactions, image annotations), which travel as DataParts so the agent
// receives them as JSON rather than as prose.
export function messageParts(text: string, dataParts?: Record<string, unknown>[]): A2APart[] {
  const parts: A2APart[] = [];
  if (text) parts.push({ kind: "text", text });
  // Wrapped, like everything else the harness puts in a `DataPart`: that dict is open and
  // reaches a session's own A2A socket, so one namespaced key keeps ours apart from a
  // foreign implementation's.
  for (const dataPart of dataParts ?? []) parts.push({ kind: "data", data: wrapPartPayload(dataPart) });
  if (parts.length === 0) parts.push({ kind: "text", text: "" });
  return parts;
}

// One turn, read from the store — so it answers whether the session that ran it is alive
// or long since reaped.
export async function turnGet(turnId: string): Promise<A2ATurn | null> {
  if (!turnId) return null;
  try {
    const data = await rpc<{ turn: A2ATurn }>("turn.get", { turn_id: turnId });
    return data.turn ?? null;
  } catch {
    return null;
  }
}

// Health, pool size and session counts. The launcher only cares that it answers at
// all — that is the daemon's readiness signal — so the payload stays untyped here
// rather than pinning a shape no caller reads.
export async function daemonStatus(options?: ApiRequestOptions & { signal?: AbortSignal }): Promise<Record<string, unknown> | null> {
  try {
    return await rpc<Record<string, unknown>>("daemon.status", {}, options);
  } catch {
    return null;
  }
}


// Every turn a session has taken, replayed from the daemon's store — so it answers
// whether the session is alive or long since reaped. Throws on failure so callers can
// distinguish a transient error (worth a retry) from a genuinely empty session.
export async function fetchSessionTurns(sessionId: string, signal?: AbortSignal): Promise<A2ATurn[]> {
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

export async function fetchSessionTurnsPage(
  sessionId: string,
  beforeRowId?: number | null,
  signal?: AbortSignal,
  limit = 400
): Promise<SessionTurnsPage> {
  const data = await rpc<{ turns?: A2ATurn[]; next_before_row_id?: number | null; has_more?: boolean }>(
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

// The outcome of resolving a pending prompt. `ok` means the decision/answer
// actually reached its waiting request. `status` distinguishes the cases so the
// caller can phrase feedback: "resolved" (delivered), "stale" (someone already
// answered it), "unknown" (no such pending request — the turn moved on or the
// server restarted), "error"/"network" (the call itself failed).
export interface ResolveResult {
  ok: boolean;
  status: "resolved" | "stale" | "unknown" | "error" | "network";
}

// Answer a gate the session is parked on. The daemon relays the response to the
// session's socket, where it lands as an `input_response` part and resumes the worker.
async function sessionRespond(payload: Record<string, unknown>): Promise<ResolveResult> {
  try {
    const data = await rpc<{ status?: unknown }>("session.respond", payload);
    const status = String(data.status ?? "");
    if (status === "resolved" || status === "stale") return { ok: true, status };
    return { ok: false, status: status === "unknown" ? "unknown" : "error" };
  } catch (error) {
    // A transport failure and a rejected request read differently to the user: one is
    // worth retrying, the other means the request is gone.
    return { ok: false, status: error instanceof TypeError ? "network" : "error" };
  }
}

// Allow-always is gone with the mid-session policy it wrote to: the only runtime
// decisions are per-call allow-once and deny.
export async function resolvePermission(
  sessionId: string,
  requestId: string,
  decision: "deny" | "allow_once"
): Promise<ResolveResult> {
  return sessionRespond({ id: sessionId, request_id: requestId, decision });
}

// Answer a pending ask_user question. `answers` is one entry per question (in
// order); each entry is the selected label string, an array of labels for
// multi-select, or the custom text the user typed. A skipped question is an empty
// entry. When `declined` is true the user dismissed the whole prompt without
// answering, and the turn is stopped.
export async function resolveQuestion(
  sessionId: string,
  requestId: string,
  answers: unknown[],
  declined = false
): Promise<ResolveResult> {
  return sessionRespond({ id: sessionId, request_id: requestId, answers, declined });
}

// Returns whether the cancel actually reached the session. A false result means the
// turn may still be running, so the caller can tell the user rather than leave them
// believing they stopped it.
export async function cancelTurn(sessionId: string): Promise<boolean> {
  try {
    await rpc("turn.cancel", { id: sessionId });
    return true;
  } catch {
    return false;
  }
}

// Terminate a session and reap its subtree — children are sessions of their own now,
// so killing a parent takes the work it created with it.
export async function deleteSession(sessionId: string): Promise<boolean> {
  try {
    await rpc("session.end", { id: sessionId });
    return true;
  } catch {
    return false;
  }
}

export async function compactSession(sessionId: string): Promise<boolean> {
  try {
    const result = await rpc<{ compacting?: boolean }>("session.compact", { id: sessionId });
    return Boolean(result?.compacting);
  } catch {
    return false;
  }
}

// Stopping one tool call is the same data-plane cancel as stopping the turn — the
// session has a single cancel — narrowed by the call the user pointed at, so a worker
// that is still mid-batch drops only that call.
export async function abortToolCall(sessionId: string, toolCallId: string): Promise<boolean> {
  try {
    await rpc("turn.cancel", { id: sessionId, tool_call_id: toolCallId });
    return true;
  } catch {
    return false;
  }
}

// Detach a still-blocking foreground shell command so it keeps running in the
// background and the agent's turn continues (the harness is notified so the model
// learns the command was backgrounded rather than finished).
export async function sendToolToBackground(sessionId: string, toolCallId: string): Promise<boolean> {
  try {
    const result = await rpc<{ backgrounded?: boolean }>("jobs.detach", {
      id: sessionId,
      tool_call_id: toolCallId,
    });
    return Boolean(result?.backgrounded);
  } catch {
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
  } catch {
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

export function subscribeGitStatus(directory: string, onStatus: (status: DirectoryValidation) => void): () => void {
  const stream = openEventStream(`/git/status/stream?directory=${encodeURIComponent(directory)}`, (raw) => {
    try {
      onStatus(JSON.parse(raw) as DirectoryValidation);
    } catch {
      // ignore malformed
    }
  });
  return () => stream.close();
}

export async function browseWorkingDirectory(): Promise<{ path: string; cancelled: boolean; error?: string }> {
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
  } catch {
    return false;
  }
}

// Open the browser's remote-debugging settings page so the user can turn the switch on, when the
// browser tool reports it is off.
export async function openBrowserRemoteDebugging(browserName = "chrome"): Promise<boolean> {
  try {
    const response = await apiFetch(
      `/browser/enable-remote-debugging?browser_name=${encodeURIComponent(browserName)}`,
      { method: "POST" }
    );
    return response.ok;
  } catch {
    return false;
  }
}

export async function fetchMessageHistory(workingDirectory: string): Promise<string[]> {
  const response = await apiFetch(`/messages/history?working_directory=${encodeURIComponent(workingDirectory)}`);
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

// A turn's control-state lives under one URI-namespaced key in `Task.metadata`, which is
// A2A's convention for an extension's attributes. The names inside it are plain: the
// namespace has already said whose they are.
export const TURN_STATE_KEY = "urn:daisy:ext:turn:v1";

// What opened a turn. `peer` is a message from another session — a peer reporting its
// result, or a parent following up — and is emphatically not the user speaking.
export type TurnKind = "user" | "peer" | "autonomous" | "compaction";

export interface TurnState {
  kind?: TurnKind;
  peerSender?: string;
  referenceTurnIds?: string[];
}

// The harness's payload inside a `DataPart`, or `{}` when the part is not ours. Same key as
// the turn metadata, because it is the same extension.
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

// Read an SSE body frame by frame. `onFrame` receives each frame's data payload and
// returns "stop" to end the read early (an in-band terminator, which the attach stream
// uses so the connection closes on `done` rather than being left to time out).
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

// A long-lived SSE subscription over `fetch`, not `EventSource`: the daemon gates every
// request on the capability token and EventSource cannot carry an Authorization header.
// Reconnection therefore becomes ours to own — EventSource retried by itself, and the
// shared bus relies on that to survive a daemon restart.
function openEventStream(path: string, onData: (raw: string) => void): { close: () => void } {
  const controller = new AbortController();
  let closed = false;

  const run = async () => {
    while (!closed) {
      try {
        const response = await apiFetch(path, {
          signal: controller.signal,
          headers: { Accept: "text/event-stream" },
        });
        if (response.ok && response.body) await pumpEventStream(response.body, onData);
      } catch {
        // A deliberate close and a dropped connection arrive the same way here; the
        // `closed` flag below is what tells them apart.
      }
      if (closed) return;
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

// A live view of a session: `session.attach`. The daemon sends a `snapshot` frame (the
// compacted transcript, same shape as fetchSessionTurns), then a `live` tail of one
// frame per emitted part, then `done` when the turn ends. This is the only response
// path — a turn is driven by `sessionSend` and observed here, whether this client sent
// the message or another one did.
// `turn` and `done` are different events and the difference matters: a session goes idle
// many times over its life, and `done` is the session itself ending. A reader that conflated
// them would either stop watching after the first turn or wait for a process to die.
export type SessionStreamFrame =
  | { kind: "snapshot"; turns: A2ATurn[] }
  | { kind: "live"; seq: number; message: A2AMessage }
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
      if (!response.ok || !response.body) return;
      await pumpEventStream(response.body, (raw) => {
        let frame: SessionStreamFrame;
        try {
          frame = JSON.parse(raw) as SessionStreamFrame;
        } catch {
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
