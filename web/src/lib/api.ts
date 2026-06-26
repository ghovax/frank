const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "/api";

// Metadata key understood by the harness A2A executor.
export const WORKING_DIRECTORY_METADATA_KEY = "harness/workingDirectory";

export async function fetchAgents(): Promise<{ name: string; label: string }[]> {
  const response = await fetch(`${API_BASE}/agents`);
  const data = await response.json();
  return data.agents;
}

export interface AgentSkill {
  id: string;
  name?: string;
  description?: string;
  tags?: string[];
  examples?: string[];
}

export interface AgentCard {
  name: string;
  description?: string;
  url: string;
  version?: string;
  skills: AgentSkill[];
}

// Discovery: every served agent's A2A AgentCard (with its skills).
export async function fetchAgentCards(): Promise<AgentCard[]> {
  const response = await fetch(`${API_BASE}/agents/cards`);
  const data = await response.json();
  return data.cards ?? [];
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

export async function fetchHomeDirectory(): Promise<string> {
  const response = await fetch(`${API_BASE}/home`);
  const data = await response.json();
  return data.home_directory;
}

export async function fetchSessions(): Promise<{ session_id: string; agent: string; title: string; created_at: string }[]> {
  const response = await fetch(`${API_BASE}/sessions`);
  const data = await response.json();
  return data.sessions;
}

// All A2A tasks for a session (context): the main turn tasks (with history +
// artifacts) and related sub-agent tasks. Used to replay a session.
export async function fetchSessionTasks(sessionId: string): Promise<A2ATask[]> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/tasks`);
  const data = await response.json();
  return data.tasks ?? [];
}

export async function resolvePermission(
  sessionId: string,
  requestId: string,
  decision: "allow" | "deny"
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

export async function setBypassPermissions(sessionId: string, bypass: boolean): Promise<void> {
  await fetch(`${API_BASE}/chat/${sessionId}/permissions/bypass`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bypass }),
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

// Sends a user message via the A2A `message/stream` JSON-RPC method and invokes
// `onResult` for each streamed A2A object (Task, status-update, artifact-update,
// message). Returns an AbortController so the caller can cancel.
export function streamA2A(
  text: string,
  agent: string,
  contextId: string | null,
  onResult: (result: A2AStreamResult) => void | Promise<void>,
  onDone: () => void,
  workingDirectory?: string
): AbortController {
  const controller = new AbortController();

  const message: A2AMessage = {
    role: "user",
    parts: [{ kind: "text", text }],
    messageId: crypto.randomUUID(),
    metadata: workingDirectory ? { [WORKING_DIRECTORY_METADATA_KEY]: workingDirectory } : undefined,
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
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const raw = line.slice(5).trim();
          if (!raw) continue;
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
