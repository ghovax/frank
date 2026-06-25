const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "/api";

export async function fetchAgents(): Promise<string[]> {
  const response = await fetch(`${API_BASE}/agents`);
  const data = await response.json();
  return data.agents;
}

export async function fetchHomeDirectory(): Promise<string> {
  const response = await fetch(`${API_BASE}/home`);
  const data = await response.json();
  return data.home_directory;
}

export async function fetchSessions(): Promise<{ session_id: string; agent: string; created_at: string }[]> {
  const response = await fetch(`${API_BASE}/sessions`);
  const data = await response.json();
  return data.sessions;
}

export async function fetchConversation(
  sessionId: string
): Promise<StreamEvent[]> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/conversation`);
  const data = await response.json();
  return data.events;
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

export async function fetchSessionStatus(
  sessionId: string
): Promise<{ session_id: string; agent: string; active: boolean }> {
  const response = await fetch(`${API_BASE}/chat/${sessionId}/status`);
  return response.json();
}

export async function fetchExecutionHistory(
  sessionId: string
): Promise<{ history: Record<string, unknown>[] }> {
  const response = await fetch(`${API_BASE}/sessions/${sessionId}/history`);
  return response.json();
}

export type StreamEventType =
  | "session"
  | "status"
  | "user"
  | "thinking"
  | "text_chunk"
  | "tool_call"
  | "tool_result"
  | "done"
  | "background_started"
  | "permission_request"
  | "tasks_updated"
  | "error"
  | "denied_injection"
  | "agent_text_chunk"
  | "agent_tool_call"
  | "agent_thinking"
  | "agent_status"
  | "agent_done";

export interface StreamEvent {
  type: StreamEventType;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}

export function streamChat(
  message: string,
  agent: string,
  sessionId: string | null,
  onEvent: (event: StreamEvent) => void,
  onDone: () => void,
  workingDirectory?: string,
): AbortController {
  const controller = new AbortController();

  const body: Record<string, string> = { message, agent };
  if (sessionId) body.session_id = sessionId;
  if (workingDirectory) body.working_directory = workingDirectory;

  fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        const errorText = await response.text().catch(() => "Unknown error");
        onEvent({ type: "error", message: `Server error (${response.status}): ${errorText}` });
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) return;

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        let currentEventType = "";

        for (const line of lines) {
          if (line.startsWith("event:")) {
            currentEventType = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            const raw = line.slice(5).trim();
            if (!raw) continue;
            try {
              const parsed = JSON.parse(raw);
              if (currentEventType) parsed.type = currentEventType;
              onEvent(parsed);
              const type = parsed.type;
              if (
                type === "tool_call" ||
                type === "tool_result" ||
                type === "error" ||
                type === "permission_request" ||
                type === "agent_tool_call" ||
                (type === "status" && parsed.code === "thinking")
              ) {
                await new Promise<void>((resolve) => {
                  requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
                });
              }
            } catch {
              // skip malformed json
            }
          }
        }
      }
    })
    .catch((error) => {
      if (error.name !== "AbortError") {
        onEvent({ type: "error", message: String(error) });
      }
    })
    .finally(onDone);

  return controller;
}
