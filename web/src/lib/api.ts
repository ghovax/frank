const API_BASE = "http://localhost:8822";

// The URL that serves a local file (and its sibling assets) for an `open_web_preview`
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

// Metadata key understood by the harness A2A executor.
export const WORKING_DIRECTORY_METADATA_KEY = "harness/workingDirectory";
export const PERMISSION_MODE_METADATA_KEY = "harness/permissionMode";

export type PermissionMode = "default" | "read_only" | "bypass";

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
  const response = await fetch(`${API_BASE}/agents${query}`);
  const data = await response.json();
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
  const response = await fetch(`${API_BASE}/agents/cards${query}`);
  const data = await response.json();
  return data.cards ?? [];
}

export interface Settings {
  api_key: string;
  exa_api_key: string;
  sandbox_enabled: boolean;
}

export interface RecentProject {
  path: string;
  name: string;
  last_used_at: string;
}

// API credentials stored in ~/.harness/configuration.yaml.
export async function fetchSettings(): Promise<Settings> {
  const response = await fetch(`${API_BASE}/settings`);
  if (!response.ok) return { api_key: "", exa_api_key: "", sandbox_enabled: true };
  return response.json();
}

export async function saveSettings(settings: Pick<Settings, "api_key" | "exa_api_key">): Promise<void> {
  await fetch(`${API_BASE}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
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
}

// Discovery: tools exposed by each configured MCP server, for the capabilities panel.
// Skills available in the selected folder — home globals plus that folder's own
// `.agents/skills`, deduped — independent of any agent, so the panel can show a
// folder's skills even when it has no agents.
export async function fetchSkills(workingDirectory?: string): Promise<AgentSkill[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const response = await fetch(`${API_BASE}/skills${query}`);
  if (!response.ok) return [];
  const data = await response.json();
  return data.skills ?? [];
}

// MCP servers are listed for the selected folder: its own `mcp.json` plus the
// home globals and the global Composio integration (deduped), never the server's
// launch directory. The subprocess pool is shared and grows as a union.
export async function fetchMcpTools(workingDirectory?: string): Promise<McpServerTools[]> {
  const query = workingDirectory
    ? `?working_directory=${encodeURIComponent(workingDirectory)}`
    : "";
  const response = await fetch(`${API_BASE}/mcp/tools${query}`);
  if (!response.ok) return [];
  const data = await response.json();
  return data.servers ?? [];
}

// Subscribe to live server events (e.g. agents changed). Returns an unsubscribe.
export function subscribeEvents(onEvent: (event: { type: string }) => void): () => void {
  const source = new EventSource(`${API_BASE}/events`);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data));
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

export async function fetchSessions(): Promise<{ session_id: string; agent: string; title: string; created_at: string; working_directory?: string; working_directory_name?: string; running?: boolean; awaiting_input?: boolean }[]> {
  const response = await fetch(`${API_BASE}/sessions`);
  const data = await response.json();
  return data.sessions;
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

export async function abortSession(sessionId: string): Promise<void> {
  await fetch(`${API_BASE}/chat/${sessionId}/abort`, { method: "POST" });
}

export async function validateWorkingDirectory(directory: string): Promise<{ valid: boolean; exists: boolean; is_directory: boolean; is_absolute: boolean; path: string }> {
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
  await fetch(`${API_BASE}/chat/${sessionId}/permissions/mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

// A2A protocol types (the subset the client consumes)

export type A2APartKind = "text" | "data" | "file";

export interface A2APart {
  kind: A2APartKind;
  text?: string;
  data?: Record<string, unknown>;
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

function parseSseFrame(frame: string): string {
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
  permissionMode: PermissionMode = "default",
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
      [PERMISSION_MODE_METADATA_KEY]: permissionMode,
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
        const errorText = await response.text().catch(() => "Unknown error");
        onResult({ kind: "status-update", taskId: "", contextId: contextId ?? "", status: { state: "failed", message: { role: "agent", parts: [{ kind: "data", data: { kind: "error", message: `Server error (${response.status}): ${errorText}` } }] } }, final: true });
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
        onResult({ kind: "status-update", taskId: "", contextId: contextId ?? "", status: { state: "failed", message: { role: "agent", parts: [{ kind: "data", data: { kind: "error", message: String(error) } }] } }, final: true });
      }
    })
    .finally(onDone);

  return controller;
}
