export type AgenticHarnessEvent =
  | { type: "session"; session_id: string }
  | { type: "text_chunk"; text: string }
  | { type: "thinking"; text: string }
  | { type: "tool_call"; name: string; args: Record<string, unknown> }
  | { type: "tool_result"; result: string }
  | { type: "done"; text: string; tool_results: string }
  | { type: "error"; message: string }
  | { type: "permission_request"; request_id: string; command: string; justification: string; risk: string }
  | { type: "background_started"; task_id: string; agent: string }

export type SessionInfo = {
  session_id: string
  agent: string
  created_at: string
  message_count: number
}

export type ToolCallInformation = {
  name: string
  arguments: Record<string, unknown>
  justification?: string
  risk?: string
  result?: string
}

export type ChatMessage = {
  role: "user" | "assistant"
  text: string
  agent?: string
  timestamp: string
  toolCalls?: ToolCallInformation[]
  error?: string
}

export class AgenticHarnessClient {
  constructor(private readonly baseUrl: string) {}

  async listAgents(): Promise<string[]> {
    const response = await fetch(`${this.baseUrl}/agents`)
    if (!response.ok) return ["main"]
    const data: { agents?: string[] } = await response.json()
    return data.agents ?? ["main"]
  }

  async sendMessage(
    message: string,
    agent: string,
    sessionId?: string,
    onEvent?: (event: AgenticHarnessEvent) => void,
    abortSignal?: AbortSignal,
  ): Promise<string> {
    const body: Record<string, unknown> = { message, agent }
    if (sessionId) body.session_id = sessionId

    const response = await fetch(`${this.baseUrl}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abortSignal,
    })

    if (!response.ok) {
      const errorText = await response.text().catch(() => "Unknown error")
      throw new Error(`Server error ${response.status}: ${errorText.slice(0, 200)}`)
    }

    const reader = response.body?.getReader()
    if (!reader) throw new Error("No response body from server")

    const decoder = new TextDecoder()
    let buffer = ""
    let resolvedSessionId = sessionId ?? ""

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split("\n")
      buffer = lines.pop() ?? ""

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed || !trimmed.startsWith("data: ")) continue
        try {
          const eventData = JSON.parse(trimmed.slice(6)) as AgenticHarnessEvent
          if (eventData.type === "session" && eventData.session_id) {
            resolvedSessionId = eventData.session_id
          }
          onEvent?.(eventData)
        } catch {
          // skip malformed JSON lines
        }
      }
    }

    return resolvedSessionId
  }

  async resolvePermission(sessionId: string, requestId: string, allow: boolean): Promise<void> {
    await fetch(`${this.baseUrl}/chat/${sessionId}/permission`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: requestId, decision: allow ? "allow" : "deny" }),
    })
  }

  async abortSession(sessionId: string): Promise<void> {
    await fetch(`${this.baseUrl}/chat/${sessionId}/abort`, { method: "POST" })
  }

  async getSessionStatus(sessionId: string): Promise<{ session_id: string; agent: string; active: boolean } | null> {
    const response = await fetch(`${this.baseUrl}/chat/${sessionId}/status`)
    if (!response.ok) return null
    return response.json()
  }
}
