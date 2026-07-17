import type { ArtifactAnnotationRecord, ArtifactImageAnnotation } from "./artifact-annotations";

// Where the harness server lives. This is resolved at runtime, not baked in at
// build time, because the desktop app can point at a local backend or a remote one
// reached through an SSH tunnel (a configurable host:port). Resolution order:
//   1. an explicit override set via `setApiBase` (persisted only in the browser build), then
//   2. a build-time default from NEXT_PUBLIC_DAISY_API_BASE, then
//   3. the conventional local harness address.
// The connection layer (profiles UI / local store) writes the override and reloads.
const DEFAULT_API_BASE =
  (typeof process !== "undefined" ? process.env.NEXT_PUBLIC_DAISY_API_BASE : "") || "http://localhost:8822";
const API_BASE_STORAGE_KEY = "daisy.apiBase";

function runningInTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function readStoredApiBase(): string {
  if (typeof window === "undefined") return DEFAULT_API_BASE;
  if (runningInTauri()) return DEFAULT_API_BASE;
  try {
    return window.localStorage.getItem(API_BASE_STORAGE_KEY) || DEFAULT_API_BASE;
  } catch {
    // localStorage can be unavailable in restricted contexts.
    return DEFAULT_API_BASE;
  }
}

let API_BASE = readStoredApiBase();

export interface ApiRequestOptions {
  apiBase?: string;
}

function apiBase(options?: ApiRequestOptions): string {
  return (options?.apiBase || API_BASE).replace(/\/+$/, "");
}

function apiUrl(path: string, options?: ApiRequestOptions): string {
  return `${apiBase(options)}${path}`;
}

function websocketUrl(path: string, options?: ApiRequestOptions): string {
  const base = apiBase(options);
  const url = new URL(path, `${base}/`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

// The address the client is currently talking to.
export function getApiBase(): string {
  return API_BASE;
}

export function terminalWebSocketUrl(options: { sessionId?: string | null; workingDirectory?: string; terminalKey?: string; locationKind?: string; locationBaseDirectory?: string; locationHostAlias?: string; rows?: number; columns?: number } = {}): string {
  const params = new URLSearchParams();
  if (options.sessionId) params.set("context_id", options.sessionId);
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
  if (sessionId) params.set("context_id", sessionId);
  if (workingDirectory) params.set("working_directory", workingDirectory);
  const query = params.toString();
  return query ? `?${query}` : "";
}

export async function listTerminals(sessionId: string | null, workingDirectory: string): Promise<TerminalInfo[]> {
  const response = await fetch(`${API_BASE}/terminals${terminalContextQuery(sessionId, workingDirectory)}`);
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
  await fetch(`${API_BASE}/terminals/${encodeURIComponent(terminalKey)}${terminalContextQuery(sessionId, workingDirectory)}`, {
    method: "DELETE",
  });
}

// Point the client at a different harness server. Persists the choice so it
// survives reloads. Callers typically reload the app afterwards so in-flight
// streams and caches restart cleanly against the new backend.
export function setApiBase(url: string): void {
  const normalized = url.trim().replace(/\/+$/, "");
  API_BASE = normalized || DEFAULT_API_BASE;
  if (typeof window === "undefined") return;
  if (runningInTauri()) return;
  try {
    if (normalized) {
      window.localStorage.setItem(API_BASE_STORAGE_KEY, normalized);
    } else {
      window.localStorage.removeItem(API_BASE_STORAGE_KEY);
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
  const response = await fetch(apiUrl(path, options));
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
    return `${API_BASE}/artifact-page/@ctx=${token}/${encoded}`;
  }
  return `${API_BASE}/artifact-page/${encoded}`;
}

// The URL that renders an external page. It is fetched and re-served from the
// backend `/artifact-proxy` route with anti-framing headers (X-Frame-Options /
// CSP frame-ancestors) stripped — otherwise sites that refuse to be framed (the
// BBC, most news sites) render as a blank, blocked frame.
export function artifactProxyUrl(url: string): string {
  if (!url) return "";
  return `${API_BASE}/artifact-proxy?url=${encodeURIComponent(url)}`;
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
  const response = await fetch(`${API_BASE}/uploads`, {
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
  const response = await fetch(`${API_BASE}/attachments/reference`, {
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
  const response = await fetch(`${API_BASE}/hosts`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.hosts) ? (data.hosts as SshHost[]) : [];
}

export async function listProjects(): Promise<Project[]> {
  const response = await fetch(`${API_BASE}/projects`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.projects) ? (data.projects as Project[]) : [];
}

export async function getProject(projectId: string): Promise<Project | null> {
  const response = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`);
  if (!response.ok) return null;
  return await response.json() as Project;
}

export async function createProject(input: ProjectCreateInput): Promise<Project> {
  const response = await fetch(`${API_BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to create project (${response.status})`);
  return await response.json() as Project;
}

export async function deleteProject(projectId: string): Promise<void> {
  await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
}

export async function createLocation(projectId: string, input: LocationInput): Promise<Location> {
  const response = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/locations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to add location (${response.status})`);
  return await response.json() as Location;
}

export async function updateLocation(locationId: string, input: LocationInput): Promise<Location> {
  const response = await fetch(`${API_BASE}/locations/${encodeURIComponent(locationId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to update location (${response.status})`);
  return await response.json() as Location;
}

export async function deleteLocation(locationId: string): Promise<void> {
  await fetch(`${API_BASE}/locations/${encodeURIComponent(locationId)}`, { method: "DELETE" });
}

// --- External A2A agents (remote agents this harness can delegate to) ---------

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
  const response = await fetch(`${API_BASE}/remote-agents`);
  if (!response.ok) throw new Error(`Failed to list remote agents (${response.status})`);
  const data = (await response.json()) as { agents: RemoteAgent[] };
  return data.agents ?? [];
}

export async function upsertRemoteAgent(input: RemoteAgentInput): Promise<void> {
  const response = await fetch(`${API_BASE}/remote-agents/${encodeURIComponent(input.name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(`Failed to save remote agent (${response.status})`);
}

export async function deleteRemoteAgent(name: string): Promise<void> {
  await fetch(`${API_BASE}/remote-agents/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export async function refreshRemoteAgent(name: string): Promise<{ health: string; error: string }> {
  const response = await fetch(`${API_BASE}/remote-agents/${encodeURIComponent(name)}/refresh`, {
    method: "POST",
  });
  if (!response.ok) throw new Error(`Failed to refresh remote agent (${response.status})`);
  return (await response.json()) as { health: string; error: string };
}

// Metadata key understood by the harness A2A executor.
// A2A convention: an extension places its attributes under one URI-namespaced key in
// the message `metadata` map, not as bare top-level keys. Mirrors DAISY_METADATA_KEY
// / Metadata in the backend's a2a_executor.
export const DAISY_METADATA_KEY = "urn:daisy:ext:turn:v1";
export const CONTENT_BLOCK_METADATA_KEY = "urn:daisy:ext:content-block:v1";

export type PermissionMode = "default" | "auto" | "read_only" | "bypass";
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

export interface AgentSpawnConfiguration {
  enabled: boolean;
}

export interface AgentConfiguration {
  id: string;
  name: string;
  title: string;
  model: string;
  provider: string;
  reasoning_effort: string;
  permission_mode: PermissionMode;
  stream_agent_progress: boolean;
  tools_enabled: string[];
  bash: AgentBashConfiguration;
  spawn_agent: AgentSpawnConfiguration;
  path: string;
}

export interface SaveAgentConfigurationPayload {
  model?: string;
  provider?: string;
  reasoning_effort?: string;
  permission_mode?: PermissionMode;
  stream_agent_progress?: boolean;
  tools_enabled?: string[];
  bash?: Partial<AgentBashConfiguration>;
  spawn_agent?: Partial<AgentSpawnConfiguration>;
}

// Agents are scoped to the selected folder: the bundled (server-shipped)
// profiles are always present as a base, then home globals, then that folder's
// own `.agents/agents` (deduped), so passing `workingDirectory` is what makes
// the list track the chosen folder rather than the server's launch directory.
export async function fetchAgents(workingDirectory?: string): Promise<{ agents: AgentSummary[]; defaultAgent: string }> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const data = await cachedDiscovery(discoveryKey("/agents", workingDirectory), () =>
    fetchJson<{ agents: AgentSummary[]; defaultAgent?: string }>(`/agents${query}`)
  );
  return { agents: data.agents, defaultAgent: data.defaultAgent ?? "" };
}

export async function fetchAgentConfiguration(agent: string, workingDirectory?: string): Promise<AgentConfiguration> {
  const query = workingDirectory ? `?working_directory=${encodeURIComponent(workingDirectory)}` : "";
  return fetchJson<AgentConfiguration>(`/agents/${encodeURIComponent(agent)}/configuration${query}`);
}

export async function saveAgentConfiguration(agent: string, payload: SaveAgentConfigurationPayload, workingDirectory?: string): Promise<AgentConfiguration> {
  const query = workingDirectory ? `?working_directory=${encodeURIComponent(workingDirectory)}` : "";
  const response = await fetch(`${API_BASE}/agents/${encodeURIComponent(agent)}/configuration${query}`, {
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
  sandbox_enabled: boolean;
  // Opt-in: inject a snapshot of the user's machine habits (frequent folders, recent
  // files, installed/running apps, most-visited sites) into the system prompt. Off by default.
  user_context_enabled: boolean;
  // Opt-in: let the agent control macOS apps via the computer-use tool. Off by default.
  computer_control_enabled: boolean;
  workspace_strategy: "none" | "branch" | "worktree";
  compaction: CompactionSettings;
  providers: Record<string, ProviderCredential>;
}

const DEFAULT_COMPACTION: CompactionSettings = {
  auto: false,
  observer_context_fraction: 0.6,
  reflector_observation_fraction: 0.3,
  keep_recent_turns: 6,
};

// Persist the Observational-Memory compaction settings (auto on/off + thresholds).
export async function updateCompactionSettings(changes: Partial<CompactionSettings>): Promise<void> {
  await fetch(`${API_BASE}/settings/compaction`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

// Toggle the opt-in user-context snapshot in the system prompt (rebuilds runtimes).
export async function updateUserContextSetting(enabled: boolean): Promise<void> {
  await fetch(`${API_BASE}/settings/user-context`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// Toggle the opt-in computer-use tool that controls macOS apps (rebuilds runtimes).
export async function updateComputerControlSetting(enabled: boolean): Promise<void> {
  await fetch(`${API_BASE}/settings/computer-control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

// Whether the server can read Full-Disk-Access-protected data (Screen Time, Safari history) —
// gates the deepest user-context signals. False on any error (e.g. non-macOS).
export async function fetchFullDiskAccess(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/system/full-disk-access`);
    if (!response.ok) return false;
    return (await response.json()).granted === true;
  } catch {
    return false;
  }
}

// Open System Settings to the Full Disk Access pane so the user can add Daisy in one hop.
export async function openFullDiskAccessSettings(): Promise<void> {
  await fetch(`${API_BASE}/system/full-disk-access/open`, { method: "POST" }).catch(() => {});
}

// Whether the app can control other apps (read the accessibility tree, synthesize input) —
// the permission the computer-use tool needs. False on any error (e.g. non-macOS).
export async function fetchAccessibility(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/system/accessibility`);
    if (!response.ok) return false;
    return (await response.json()).granted === true;
  } catch {
    return false;
  }
}

// Trigger the system Accessibility prompt and open its pane so the user can grant Daisy.
export async function openAccessibilitySettings(): Promise<void> {
  await fetch(`${API_BASE}/system/accessibility/open`, { method: "POST" }).catch(() => {});
}

// Trigger the system Screen Recording prompt and open its pane so the user can grant Daisy.
export async function openScreenRecordingSettings(): Promise<void> {
  await fetch(`${API_BASE}/system/screen-recording/open`, { method: "POST" }).catch(() => {});
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

export interface FilesystemLease {
  owner_session_id: string;
  scope: "file" | "worktree";
  path: string;
  working_directory: string;
  description: string;
  acquired_at: number;
}

// API credentials stored in ~/.daisy/configuration.yaml.
export async function fetchSettings(): Promise<Settings> {
  const response = await fetch(`${API_BASE}/settings`);
  if (!response.ok) {
    return { permission_mode: "default", exa_api_key: "", composio_api_key: "", jina_api_key: "", firecrawl_api_key: "", web_fetch_proxy_url: "", sandbox_enabled: true, user_context_enabled: false, computer_control_enabled: false, workspace_strategy: "none", compaction: DEFAULT_COMPACTION, providers: {} };
  }
  return (await response.json()) as Settings;
}

export interface SaveSettingsPayload {
  permission_mode?: PermissionMode;
  sandbox_enabled?: boolean;
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
  await fetch(`${API_BASE}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

// The model catalog for the picker (with per-model availability) and the provider
// registry.
export async function fetchModels(): Promise<ModelsResponse> {
  const response = await fetch(`${API_BASE}/models`);
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
  const response = await fetch(`${API_BASE}/models/recent`);
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
  const response = await fetch(`${API_BASE}/auth/chatgpt`);
  if (!response.ok) return { signed_in: false, email: "", usage: null };
  return response.json();
}

// Begin sign-in: the server binds its loopback callback and returns the OpenAI
// authorize URL to open in a browser. Completion arrives via a `settings_changed`
// broadcast (or by re-polling fetchChatGPTAuthStatus).
export async function startChatGPTLogin(): Promise<{ authorize_url: string }> {
  const response = await fetch(`${API_BASE}/auth/chatgpt/start`, { method: "POST" });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || "Could not start ChatGPT sign-in.");
  }
  return response.json();
}

export async function signOutChatGPT(): Promise<void> {
  await fetch(`${API_BASE}/auth/chatgpt`, { method: "DELETE" });
}

export async function fetchArtifactAnnotations(contextId: string): Promise<ArtifactAnnotationRecord[]> {
  if (!contextId) return [];
  const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/artifact-annotations`);
  if (!response.ok) return [];
  const data = await response.json();
  return Array.isArray(data.records) ? data.records as ArtifactAnnotationRecord[] : [];
}

export async function saveArtifactAnnotations(contextId: string, record: ArtifactAnnotationRecord): Promise<void> {
  if (!contextId) return;
  await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/artifact-annotations`, {
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
  await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/artifact-annotations?${query}`, {
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
  return `${API_BASE}/artifact-bytes?${params.toString()}`;
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
  const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/artifacts?scope=${scope}`);
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
  const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/artifacts/versions?${params.toString()}`);
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
  const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/artifacts/diff?${params.toString()}`);
  if (!response.ok) return "";
  const data = await response.json();
  return String(data.diff ?? "");
}

export async function restoreArtifact(
  contextId: string,
  options: { locationUri: string; gitDirectory: string; workTree: string; relativePath: string; commitSha: string },
): Promise<boolean> {
  if (!contextId) return false;
  const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/artifacts/restore`, {
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

export async function setSandboxEnabled(enabled: boolean): Promise<void> {
  await fetch(`${API_BASE}/settings/sandbox`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
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
// All subscribers share ONE EventSource. Opening one per subscriber (many
// components subscribe) quickly exhausted the browser's ~6-connections-per-host
// limit with long-lived SSE streams, starving every other request to the server —
// most visibly, a user-triggered POST could never get a connection and hung
// forever. A single stream fans out to all local listeners instead.
const eventListeners = new Set<(event: { type: string }) => void>();
let sharedEventSource: EventSource | null = null;

function ensureEventSource(): void {
  if (sharedEventSource) return;
  const source = new EventSource(`${API_BASE}/events`);
  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.type === "agents_changed") invalidateDiscoveryCache();
      eventListeners.forEach((listener) => listener(event));
    } catch {
      // ignore malformed
    }
  };
  sharedEventSource = source;
}

export function subscribeEvents(onEvent: (event: { type: string }) => void): () => void {
  eventListeners.add(onEvent);
  ensureEventSource();
  return () => {
    eventListeners.delete(onEvent);
    // Close the shared stream once nothing is listening, so it reopens cleanly
    // (and against a possibly-changed API base) when a subscriber returns.
    if (eventListeners.size === 0 && sharedEventSource) {
      sharedEventSource.close();
      sharedEventSource = null;
    }
  };
}

// The default project shown before the user picks anything — the server provides
// its folder name so the selector never has to derive one.
export async function fetchHomeDirectory(): Promise<{ path: string; name: string }> {
  const response = await fetch(`${API_BASE}/home`);
  const data = await response.json();
  return { path: String(data.path ?? ""), name: String(data.name ?? "") };
}

// Best-effort home directory of an SSH host (for prefilling a location's base directory).
// Returns "" if the host is unknown/unreachable — the field then stays empty for manual entry.
export async function fetchHostHomeDirectory(alias: string): Promise<string> {
  try {
    const response = await fetch(`${API_BASE}/hosts/${encodeURIComponent(alias)}/home`);
    if (!response.ok) return "";
    const data = await response.json();
    return String(data.path ?? "");
  } catch {
    return "";
  }
}

export async function fetchSessions(options?: ApiRequestOptions): Promise<{
  session_id: string;
  project_id?: string;
  agent: string;
  title: string;
  created_at: string;
  working_directory?: string;
  runtime_working_directory?: string;
  workspace_strategy?: "none" | "branch" | "worktree";
  workspace_path?: string;
  workspace_branch?: string;
  source_repository_root?: string;
  runtime_repository_root?: string;
  workspace_head?: string;
  workspace_error?: string;
  running?: boolean;
  awaiting_input?: boolean;
  permission_mode?: PermissionMode;
  input_draft?: string;
  filesystem_leases?: FilesystemLease[];
}[]> {
  const response = await fetch(apiUrl("/sessions", options));
  const data = await response.json();
  return data.sessions;
}

export async function fetchFilesystemLeases(): Promise<FilesystemLease[]> {
  const response = await fetch(`${API_BASE}/filesystem/leases`);
  if (!response.ok) return [];
  const data = await response.json();
  return data.leases ?? [];
}

// All A2A tasks for a session (context): the main turn tasks (with history +
// artifacts) and related agent tasks. Used to replay a session. Throws on a
// non-OK response so callers can distinguish a transient failure (worth a retry)
// from a genuinely empty session — `fetch` itself only rejects on network errors.
export async function fetchSessionTasks(sessionId: string, signal?: AbortSignal): Promise<A2ATask[]> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/tasks`, { signal });
  if (!response.ok) throw new Error(`Failed to load session tasks (${response.status})`);
  const data = await response.json();
  return data.tasks ?? [];
}

export interface SessionTasksPage {
  tasks: A2ATask[];
  next_before_row_id: number | null;
  has_more: boolean;
}

export async function fetchSessionDraft(sessionId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/draft`);
  if (!response.ok) return "";
  const data = await response.json();
  return String(data.input_draft ?? "");
}

export async function saveSessionDraft(sessionId: string, inputDraft: string): Promise<void> {
  if (!sessionId) return;
  await fetch(`${API_BASE}/sessions/${sessionId}/draft`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input_draft: inputDraft }),
  });
}

export async function fetchSessionTasksPage(
  sessionId: string,
  beforeRowId?: number | null,
  signal?: AbortSignal,
  limit = 400
): Promise<SessionTasksPage> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeRowId != null) params.set("before_row_id", String(beforeRowId));
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/tasks/page?${params.toString()}`, { signal });
  if (!response.ok) throw new Error(`Failed to load session task page (${response.status})`);
  const data = await response.json();
  return {
    tasks: data.tasks ?? [],
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

async function postResolve(url: string, payload: Record<string, unknown>): Promise<ResolveResult> {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) return { ok: false, status: "error" };
    const data = await response.json().catch(() => ({}));
    const status = String((data as { status?: unknown }).status ?? "");
    if (status === "resolved" || status === "stale") return { ok: true, status };
    return { ok: false, status: status === "unknown" ? "unknown" : "error" };
  } catch {
    return { ok: false, status: "network" };
  }
}

export async function resolvePermission(
  sessionId: string,
  requestId: string,
  decision: "deny" | "allow_once" | "allow_always"
): Promise<ResolveResult> {
  return postResolve(`${API_BASE}/chat/${sessionId}/permission`, { request_id: requestId, decision });
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
  return postResolve(`${API_BASE}/chat/${sessionId}/question`, { request_id: requestId, answers, declined });
}

export async function steerSession(sessionId: string, message: string): Promise<boolean> {
  const response = await fetch(`${API_BASE}/chat/${sessionId}/steer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) return false;
  const data = await response.json();
  return !!data.queued;
}

// Returns whether the abort request actually reached the server. A false result
// means the turn may still be running, so the caller can tell the user rather than
// leave them believing they stopped it.
export async function abortSession(sessionId: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/chat/${sessionId}/abort`, { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}

// Permanently delete a session and all its tasks on the server.
export async function deleteSession(sessionId: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    return response.ok;
  } catch {
    return false;
  }
}

export async function compactSession(sessionId: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/chat/${sessionId}/compact`, { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}

export async function abortToolCall(sessionId: string, toolCallId: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/chat/${encodeURIComponent(sessionId)}/tools/${encodeURIComponent(toolCallId)}/abort`, { method: "POST" });
    return response.ok;
  } catch {
    return false;
  }
}

export async function cancelAgent(sessionId: string, taskIdentifier: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/chat/${encodeURIComponent(sessionId)}/agents/${encodeURIComponent(taskIdentifier)}/abort`, { method: "POST" });
    if (!response.ok) return false;
    const result = await response.json() as { status?: string };
    return result.status === "aborted";
  } catch {
    return false;
  }
}

// Detach a still-blocking foreground shell command so it keeps running in the
// background and the agent's turn continues (the harness is notified so the model
// learns the command was backgrounded rather than finished).
export async function sendToolToBackground(sessionId: string, toolCallId: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/chat/${encodeURIComponent(sessionId)}/tools/${encodeURIComponent(toolCallId)}/background`, { method: "POST" });
    return response.ok;
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
    const response = await fetch(`${API_BASE}/chat/${encodeURIComponent(sessionId)}/background`);
    if (!response.ok) return [];
    const data = await response.json();
    return Array.isArray(data.jobs) ? data.jobs as BackgroundJob[] : [];
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
  const response = await fetch(`${API_BASE}/directory/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ directory }),
  });
  return response.json();
}

export function subscribeGitStatus(directory: string, onStatus: (status: DirectoryValidation) => void): () => void {
  const source = new EventSource(`${API_BASE}/git/status/stream?directory=${encodeURIComponent(directory)}`);
  source.onmessage = (message) => {
    try {
      onStatus(JSON.parse(message.data) as DirectoryValidation);
    } catch {
      // ignore malformed
    }
  };
  return () => source.close();
}

export async function browseWorkingDirectory(): Promise<{ path: string; cancelled: boolean; error?: string }> {
  const response = await fetch(`${API_BASE}/directory/browse`, { method: "POST" });
  return response.json();
}

export async function revealInFinder(path: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/directory/reveal`, {
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
    const response = await fetch(
      `${API_BASE}/browser/enable-remote-debugging?browser_name=${encodeURIComponent(browserName)}`,
      { method: "POST" }
    );
    return response.ok;
  } catch {
    return false;
  }
}

export async function setPermissionMode(sessionId: string, mode: PermissionMode): Promise<void> {
  const response = await fetch(`${API_BASE}/chat/${sessionId}/permissions/mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!response.ok) throw new Error(`Failed to save permission mode (${response.status})`);
}

export async function fetchMessageHistory(workingDirectory: string): Promise<string[]> {
  const response = await fetch(`${API_BASE}/messages/history?working_directory=${encodeURIComponent(workingDirectory)}`);
  if (!response.ok) throw new Error(`Failed to fetch message history (${response.status})`);
  const data = await response.json();
  return data.messages as string[];
}

export async function saveMessageHistory(workingDirectory: string, message: string): Promise<void> {
  const response = await fetch(`${API_BASE}/messages/history`, {
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

export interface A2AErrorData {
  kind: "error";
  code?: string;
  title?: string;
  message?: string;
  status?: number;
}

export interface A2AMessage {
  role: "user" | "agent";
  parts: A2APart[];
  messageId?: string;
  contextId?: string;
  taskId?: string;
  referenceTaskIds?: string[];
  metadata?: Record<string, unknown>;
}

export interface A2AArtifact {
  artifactId?: string;
  name?: string;
  parts: A2APart[];
}

export interface A2ATaskStatus {
  state: string;
  message?: A2AMessage;
  timestamp?: string;
}

export interface A2ATask {
  id: string;
  contextId: string;
  kind?: string;
  status: A2ATaskStatus;
  artifacts?: A2AArtifact[];
  history?: A2AMessage[];
  metadata?: Record<string, unknown>;
}

export interface A2AStatusUpdate {
  kind: "status-update";
  taskId: string;
  contextId: string;
  status: A2ATaskStatus;
  final?: boolean;
}

export interface A2AArtifactUpdate {
  kind: "artifact-update";
  taskId: string;
  contextId: string;
  artifact: A2AArtifact;
  lastChunk?: boolean;
}

// Any object the message/stream method yields.
export type A2AStreamResult =
  | (A2ATask & { kind?: "task" })
  | A2AStatusUpdate
  | A2AArtifactUpdate
  | (A2AMessage & { kind: "message" });

export function parseSseFrame(frame: string): string {
  return frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n")
    .trim();
}

async function emitSseFrame(
  frame: string,
  onResult: (result: A2AStreamResult) => void | Promise<void>
) {
  const raw = parseSseFrame(frame);
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    const result = parsed.result ?? parsed;
    if (result && typeof result === "object") {
      await onResult(result as A2AStreamResult);
    }
  } catch {
    // skip malformed json
  }
}

// Sends a user message via the A2A `message/stream` JSON-RPC method and invokes
// `onResult` for each streamed A2A object (Task, status-update, artifact-update,
// message). Returns an AbortController so the caller can cancel.
export function streamA2A(
  text: string,
  agent: string,
  contextId: string | null,
  onResult: (result: A2AStreamResult) => void | Promise<void>,
  onDone: () => void,
  workingDirectory?: string,
  workspaceStrategy: WorkspaceStrategy = "none",
  permissionMode: PermissionMode = "default",
  messageId: string = crypto.randomUUID(),
  // Optional structured payloads carried as typed DataParts alongside (or instead
  // of) the text — e.g. attachments plus artifact-image annotations.
  dataParts?: Record<string, unknown>[],
  // The project this turn runs in; the server resolves its locations so the agent can
  // address any of them per tool call.
  projectId: string = ""
): AbortController {
  const controller = new AbortController();

  const parts: A2APart[] = [];
  if (text) parts.push({ kind: "text", text });
  for (const dataPart of dataParts ?? []) {
    parts.push({ kind: "data", data: dataPart });
  }
  if (parts.length === 0) parts.push({ kind: "text", text: "" });

  const message: A2AMessage = {
    role: "user",
    parts,
    messageId,
    metadata: {
      [DAISY_METADATA_KEY]: {
        ...(projectId ? { projectId } : {}),
        ...(workingDirectory ? { workingDirectory } : {}),
        workspaceStrategy,
        permissionMode,
      },
    },
  };
  if (contextId) message.contextId = contextId;

  const body = {
    jsonrpc: "2.0",
    id: crypto.randomUUID(),
    method: "message/stream",
    params: { message },
  };

  // Each agent is its own A2A endpoint; selecting an agent means addressing it.
  fetch(`${API_BASE}/a2a/agents/${agent}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok || !response.body) {
        onResult({ kind: "status-update", taskId: "", contextId: contextId ?? "", status: { state: "failed", message: { role: "agent", parts: [{ kind: "data", data: { kind: "error", code: "server_error", title: "Server request failed", message: "Daisy could not start the turn. Check the server log and try again.", status: response.status } }] } }, final: true });
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          await emitSseFrame(frame, onResult);
        }
        if (done) break;
      }
      if (buffer.trim()) {
        await emitSseFrame(buffer, onResult);
      }
    })
    .catch((error) => {
      if (error.name !== "AbortError") {
        onResult({ kind: "status-update", taskId: "", contextId: contextId ?? "", status: { state: "failed", message: { role: "agent", parts: [{ kind: "data", data: { kind: "error", code: "network_error", title: "Could not reach Daisy", message: "The browser lost its connection to the Daisy server. Check that the server is still running and retry." } }] } }, final: true });
      }
    })
    .finally(onDone);

  return controller;
}

// A live, read-only view of a running session's structured parts — for a viewer
// that isn't driving the turn. The server sends a `snapshot` frame (the compacted
// transcript, same shape as fetchSessionTasks), then a `live` tail of one frame per
// emitted part in the same agent-message shape the driver consumes, then a `done`
// frame. Replaces per-second polling + full re-replay (O(N)/s) with O(delta) live
// updates.
export type SessionStreamFrame =
  | { kind: "snapshot"; tasks: A2ATask[] }
  | { kind: "live"; seq: number; message: A2AMessage }
  | { kind: "done" };

export function subscribeSessionStream(
  sessionId: string,
  onFrame: (frame: SessionStreamFrame) => void,
  onDone: () => void,
): { abort: () => void } {
  const controller = new AbortController();
  fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/stream`, {
    signal: controller.signal,
    headers: { Accept: "text/event-stream" },
  })
    .then(async (response) => {
      if (!response.ok || !response.body) {
        onDone();
        return;
      }
      const reader = response.body.getReader();
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
          try {
            const parsed = JSON.parse(raw) as SessionStreamFrame;
            onFrame(parsed);
            if (parsed.kind === "done") {
              controller.abort();
              return;
            }
          } catch {
            // skip malformed frame
          }
        }
        if (done) break;
      }
    })
    .catch((error) => {
      if (error.name !== "AbortError") {
        // swallow — onDone still fires via finally
      }
    })
    .finally(onDone);

  return { abort: () => controller.abort() };
}
