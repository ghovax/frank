// Where the harness server lives. This is resolved at runtime, not baked in at
// build time, because the desktop app can point at a local backend or a remote one
// reached through an SSH tunnel (a configurable host:port). Resolution order:
//   1. an explicit override set via `setApiBase` (persisted to localStorage), then
//   2. a build-time default from NEXT_PUBLIC_DAISY_API_BASE, then
//   3. the conventional local harness address.
// The connection layer (profiles UI / local store) writes the override and reloads.
const DEFAULT_API_BASE =
  process.env.NEXT_PUBLIC_DAISY_API_BASE || "http://localhost:8822";
const API_BASE_STORAGE_KEY = "daisy.apiBase";

function readStoredApiBase(): string {
  if (typeof window === "undefined") return DEFAULT_API_BASE;
  try {
    return window.localStorage.getItem(API_BASE_STORAGE_KEY) || DEFAULT_API_BASE;
  } catch {
    // localStorage can be unavailable in restricted contexts.
    return DEFAULT_API_BASE;
  }
}

let API_BASE = readStoredApiBase();

// The address the client is currently talking to.
export function getApiBase(): string {
  return API_BASE;
}

// Point the client at a different harness server. Persists the choice so it
// survives reloads. Callers typically reload the app afterwards so in-flight
// streams and caches restart cleanly against the new backend.
export function setApiBase(url: string): void {
  const normalized = url.trim().replace(/\/+$/, "");
  API_BASE = normalized || DEFAULT_API_BASE;
  if (typeof window === "undefined") return;
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

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
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

// The URL that serves a local file (and its sibling assets) for an `open_preview`
// artifact — the backend `/preview/<abs path>` route reads the file off disk and,
// for HTML, injects the widget runtime. Each path segment is encoded but the
// slashes are kept, so relative assets inside a previewed page still resolve.
export function filePreviewUrl(path: string): string {
  const normalized = path.replace(/^\/+/, "");
  if (!normalized) return "";
  const encoded = normalized.split("/").map(encodeURIComponent).join("/");
  return `${API_BASE}/preview/${encoded}`;
}

// The URL that previews an external page. It is fetched and re-served from the
// backend `/preview-proxy` route with anti-framing headers (X-Frame-Options /
// CSP frame-ancestors) stripped — otherwise sites that refuse to be framed (the
// BBC, most news sites) render as a blank, blocked frame.
export function proxyPreviewUrl(url: string): string {
  if (!url) return "";
  return `${API_BASE}/preview-proxy?url=${encodeURIComponent(url)}`;
}

export interface ResearchUpload {
  upload_id: string;
  title: string;
  filename: string;
  path: string;
  mime_type: string;
  size: number;
  sha256: string;
  source: {
    origin_channel: "upload";
    source_kind: "document";
    title: string;
    path: string;
    metadata: Record<string, unknown>;
  };
}

export async function uploadResearchFile(file: File): Promise<ResearchUpload> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${API_BASE}/research/uploads`, {
    method: "POST",
    body,
  });
  if (!response.ok) throw new Error(`Failed to upload ${file.name} (${response.status})`);
  return await response.json() as ResearchUpload;
}

// Metadata key understood by the harness A2A executor.
export const WORKING_DIRECTORY_METADATA_KEY = "harness/workingDirectory";
export const WORKSPACE_STRATEGY_METADATA_KEY = "harness/workspaceStrategy";
export const PERMISSION_MODE_METADATA_KEY = "harness/permissionMode";
export const SELECTED_MODEL_METADATA_KEY = "harness/selectedModel";

export type PermissionMode = "default" | "auto" | "read_only" | "bypass";
export type WorkspaceStrategy = "none" | "branch" | "worktree";

export interface AgentSummary {
  id: string;
  name: string;
  title?: string;
}

// Agents are scoped to the selected folder (home globals plus that folder's own
// `.agents/agents`, deduped), so passing `workingDirectory` is what makes the
// list track the chosen folder rather than the server's launch directory.
export async function fetchAgents(workingDirectory?: string): Promise<AgentSummary[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const data = await cachedDiscovery(discoveryKey("/agents", workingDirectory), () =>
    fetchJson<{ agents: AgentSummary[] }>(`/agents${query}`)
  );
  return data.agents;
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

export interface DotsOCRSettings {
  enabled: boolean;
  mode: "local" | "remote";
  endpoint: string;
  api_key: string;
  model_name: string;
  prompt_mode: string;
  timeout_seconds: number;
}

const DEFAULT_DOTS_OCR_SETTINGS: DotsOCRSettings = {
  enabled: false,
  mode: "local",
  endpoint: "",
  api_key: "",
  model_name: "rednote-hilab/dots.mocr",
  prompt_mode: "prompt_layout_all_en",
  timeout_seconds: 900,
};

export interface Settings {
  exa_api_key: string;
  composio_api_key: string;
  dots_ocr: DotsOCRSettings;
  sandbox_enabled: boolean;
  workspace_strategy: "none" | "branch" | "worktree";
  selected_model: string;
  providers: Record<string, ProviderCredential>;
}

export interface ModelOption {
  id: string;
  name: string;
  provider: string;
  available: boolean;
}

export interface ProviderOption {
  id: string;
  name: string;
  openai_compatible: boolean;
}

export interface ModelsResponse {
  models: ModelOption[];
  providers: ProviderOption[];
  selected_model: string;
}

export interface RecentProject {
  path: string;
  name: string;
  last_used_at: string;
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
    return { exa_api_key: "", composio_api_key: "", dots_ocr: DEFAULT_DOTS_OCR_SETTINGS, sandbox_enabled: true, workspace_strategy: "none", selected_model: "", providers: {} };
  }
  const settings = await response.json();
  return { ...settings, dots_ocr: { ...DEFAULT_DOTS_OCR_SETTINGS, ...(settings.dots_ocr ?? {}) } };
}

export interface SaveSettingsPayload {
  exa_api_key: string;
  composio_api_key: string;
  dots_ocr: DotsOCRSettings;
  provider_keys: Record<string, string>;
  provider_base_urls: Record<string, string>;
  selected_model: string;
  workspace_strategy: "none" | "branch" | "worktree";
}

export async function saveSettings(settings: SaveSettingsPayload): Promise<void> {
  await fetch(`${API_BASE}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

// The model catalog for the picker (with per-model availability) and the provider
// registry, plus the configured selected model.
export async function fetchModels(): Promise<ModelsResponse> {
  const response = await fetch(`${API_BASE}/models`);
  if (!response.ok) return { models: [], providers: [], selected_model: "" };
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

// Set or clear a per-session model override (provider/model id, or "" for the
// global default). Persists to the sessions table and rebuilds the runtime.
export async function setSessionModel(contextId: string, model: string): Promise<void> {
  await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/model`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
}

export async function fetchSessionModel(contextId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/sessions/${encodeURIComponent(contextId)}/model`);
  if (!response.ok) return "";
  const data = await response.json();
  return data.model ?? "";
}

export async function setSandboxEnabled(enabled: boolean): Promise<void> {
  await fetch(`${API_BASE}/settings/sandbox`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export async function fetchRecentProjects(): Promise<RecentProject[]> {
  const response = await fetch(`${API_BASE}/projects/recent`);
  if (!response.ok) return [];
  const data = await response.json();
  return data.projects ?? [];
}

// Records a selection and returns the server's canonical {path, name} for it —
// the server owns the folder-name derivation, so the client never parses paths.
export async function recordRecentProject(path: string): Promise<{ path: string; name: string } | null> {
  const response = await fetch(`${API_BASE}/projects/recent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!response.ok) return null;
  const data = await response.json();
  return data.saved ? { path: data.path, name: data.name } : null;
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
export function subscribeEvents(onEvent: (event: { type: string }) => void): () => void {
  const source = new EventSource(`${API_BASE}/events`);
  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.type === "agents_changed") invalidateDiscoveryCache();
      onEvent(event);
    } catch {
      // ignore malformed
    }
  };
  return () => source.close();
}

// The default project shown before the user picks anything — the server provides
// its folder name so the selector never has to derive one.
export async function fetchHomeDirectory(): Promise<{ path: string; name: string }> {
  const response = await fetch(`${API_BASE}/home`);
  const data = await response.json();
  return { path: String(data.path ?? ""), name: String(data.name ?? "") };
}

export async function fetchSessions(): Promise<{
  session_id: string;
  agent: string;
  title: string;
  created_at: string;
  working_directory?: string;
  working_directory_name?: string;
  runtime_working_directory?: string;
  runtime_working_directory_name?: string;
  workspace_strategy?: "none" | "branch" | "worktree";
  workspace_path?: string;
  workspace_branch?: string;
  source_repository_root?: string;
  runtime_repository_root?: string;
  workspace_head?: string;
  workspace_error?: string;
  running?: boolean;
  awaiting_input?: boolean;
  model?: string;
  permission_mode?: PermissionMode;
  filesystem_leases?: FilesystemLease[];
}[]> {
  const response = await fetch(`${API_BASE}/sessions`);
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
// artifacts) and related sub-agent tasks. Used to replay a session. Throws on a
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

export async function resolvePermission(
  sessionId: string,
  requestId: string,
  decision: "deny" | "allow_once" | "allow_always"
): Promise<void> {
  await fetch(`${API_BASE}/chat/${sessionId}/permission`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: requestId, decision }),
  });
}

// Answer a pending ask_user question. `answers` is one entry per question (in
// order); each entry is the selected label string, an array of labels for
// multi-select, or the custom text the user typed.
export async function resolveQuestion(
  sessionId: string,
  requestId: string,
  answers: unknown[]
): Promise<void> {
  await fetch(`${API_BASE}/chat/${sessionId}/question`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: requestId, answers }),
  });
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

export async function abortSession(sessionId: string): Promise<void> {
  await fetch(`${API_BASE}/chat/${sessionId}/abort`, { method: "POST" });
}

export async function abortToolCall(sessionId: string, toolCallId: string): Promise<void> {
  await fetch(`${API_BASE}/chat/${encodeURIComponent(sessionId)}/tools/${encodeURIComponent(toolCallId)}/abort`, { method: "POST" });
}

export async function validateWorkingDirectory(directory: string): Promise<{
  valid: boolean;
  exists: boolean;
  is_directory: boolean;
  is_absolute: boolean;
  is_git_repository: boolean;
  repository_root: string;
  path: string;
}> {
  const response = await fetch(`${API_BASE}/directory/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ directory }),
  });
  return response.json();
}

export async function browseWorkingDirectory(): Promise<{ path: string; cancelled: boolean; error?: string }> {
  const response = await fetch(`${API_BASE}/directory/browse`, { method: "POST" });
  return response.json();
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
  selectedModel: string = "",
  // Optional structured payload carried as a typed DataPart alongside (or instead
  // of) the text — e.g. a widget interaction posted back to the agent.
  dataPart?: Record<string, unknown>
): AbortController {
  const controller = new AbortController();

  const parts: A2APart[] = [];
  if (text) parts.push({ kind: "text", text });
  if (dataPart) parts.push({ kind: "data", data: dataPart });
  if (parts.length === 0) parts.push({ kind: "text", text: "" });

  const message: A2AMessage = {
    role: "user",
    parts,
    messageId: crypto.randomUUID(),
    metadata: {
      ...(workingDirectory ? { [WORKING_DIRECTORY_METADATA_KEY]: workingDirectory } : {}),
      [WORKSPACE_STRATEGY_METADATA_KEY]: workspaceStrategy,
      [PERMISSION_MODE_METADATA_KEY]: permissionMode,
      ...(selectedModel ? { [SELECTED_MODEL_METADATA_KEY]: selectedModel } : {}),
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
