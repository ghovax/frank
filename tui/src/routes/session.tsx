import { batch, createSignal, For, Show, createEffect, onCleanup, onMount } from "solid-js"
import {
  AgenticHarnessClient,
  type AgenticHarnessEvent,
  type ChatMessage,
  type ToolCallInformation,
} from "../client"
import { Spinner } from "../component/spinner"
import { Prompt } from "../component/prompt"
import { StatusBar } from "../component/status-bar"
import { ChatMessage as ChatMessageComponent } from "../component/chat-message"
import { PermissionDialog, type PermissionRequest } from "../component/permission-dialog"
import { useTheme, agentColors } from "../theme"

function formatTimestamp(): string {
  const now = new Date()
  const hours = now.getHours().toString().padStart(2, "0")
  const minutes = now.getMinutes().toString().padStart(2, "0")
  const seconds = now.getSeconds().toString().padStart(2, "0")
  return `${hours}:${minutes}:${seconds}`
}

export function Session(props: {
  serverUrl: string
  client: AgenticHarnessClient
  initialMessage: string
  initialAgent: string
  onReturnHome: () => void
}) {
  const theme = useTheme()

  const [messages, setMessages] = createSignal<ChatMessage[]>([])
  const [sessionId, setSessionId] = createSignal<string | undefined>()
  const [isStreaming, setIsStreaming] = createSignal(false)
  const [streamingText, setStreamingText] = createSignal("")
  const [streamingToolCalls, setStreamingToolCalls] = createSignal<ToolCallInformation[]>([])
  const [currentAgent, setCurrentAgent] = createSignal(props.initialAgent)
  const [availableAgents, setAvailableAgents] = createSignal<string[]>([props.initialAgent])
  const [permissionRequest, setPermissionRequest] = createSignal<PermissionRequest | null>(null)
  const [connectionError, setConnectionError] = createSignal<string | null>(null)

  let abortController: AbortController | null = null
  let scrollableContainer: any

  onMount(async () => {
    try {
      const agentList = await props.client.listAgents()
      if (agentList.length > 0) setAvailableAgents(agentList)
    } catch {
      // fall back to default
    }
    sendMessage(props.initialMessage)
  })

  onCleanup(() => {
    abortController?.abort()
  })

  function scrollToBottom() {
    setTimeout(() => {
      if (scrollableContainer && !scrollableContainer.isDestroyed) {
        scrollableContainer.scrollTo(scrollableContainer.scrollHeight)
      }
    }, 50)
  }

  function sendMessage(text: string) {
    batch(() => {
      setConnectionError(null)
      setPermissionRequest(null)
      setStreamingText("")
      setStreamingToolCalls([])
      setIsStreaming(true)
    })

    const userMessage: ChatMessage = {
      role: "user",
      text,
      agent: currentAgent(),
      timestamp: formatTimestamp(),
    }
    setMessages((previousMessages) => [...previousMessages, userMessage])
    scrollToBottom()

    abortController = new AbortController()

    props.client
      .sendMessage(text, currentAgent(), sessionId(), handleServerEvent, abortController.signal)
      .then((newSessionId) => {
        if (newSessionId) setSessionId(newSessionId)
        finalizeAssistantMessage()
      })
      .catch((error: Error) => {
        if (error.name === "AbortError") return
        batch(() => {
          setIsStreaming(false)
          setConnectionError(error.message ?? "Connection to server failed")
        })
      })
  }

  function handleServerEvent(event: AgenticHarnessEvent) {
    switch (event.type) {
      case "session":
        setSessionId(event.session_id)
        break

      case "text_chunk":
        setStreamingText((previousText) => previousText + event.text)
        break

      case "thinking":
        setStreamingText((previousText) => `${previousText}\n[Thinking] ${event.text}`)
        break

      case "tool_call": {
        const toolCall: ToolCallInformation = {
          name: event.name,
          arguments: event.args,
          justification: (event.args as Record<string, unknown>).justification as string | undefined,
          risk: (event.args as Record<string, unknown>).risk as string | undefined,
        }
        setStreamingToolCalls((previousCalls) => [...previousCalls, toolCall])
        break
      }

      case "tool_result": {
        setStreamingToolCalls((previousCalls) => {
          const updatedCalls = [...previousCalls]
          const lastCall = updatedCalls[updatedCalls.length - 1]
          if (lastCall) {
            lastCall.result = event.result.slice(0, 500)
          }
          return updatedCalls
        })
        break
      }

      case "error":
        setConnectionError(event.message)
        break

      case "permission_request":
        setPermissionRequest({
          requestId: event.request_id,
          command: event.command,
          justification: event.justification,
          risk: event.risk,
        })
        break

      case "done":
        finalizeAssistantMessage()
        break
    }
    scrollToBottom()
  }

  function finalizeAssistantMessage() {
    batch(() => {
      const currentText = streamingText()
      const currentCalls = streamingToolCalls()

      setMessages((previousMessages) => {
        const lastMessage = previousMessages[previousMessages.length - 1]
        if (lastMessage && lastMessage.role === "assistant") {
          return [
            ...previousMessages.slice(0, -1),
            {
              ...lastMessage,
              text: currentText || lastMessage.text,
              toolCalls: currentCalls.length > 0 ? currentCalls : lastMessage.toolCalls,
            },
          ]
        }
        const assistantMessage: ChatMessage = {
          role: "assistant",
          text: currentText,
          agent: currentAgent(),
          timestamp: formatTimestamp(),
          toolCalls: currentCalls.length > 0 ? currentCalls : undefined,
        }
        return [...previousMessages, assistantMessage]
      })

      setIsStreaming(false)
      setStreamingText("")
      setStreamingToolCalls([])
    })
    scrollToBottom()
  }

  async function handlePermissionResponse(allow: boolean) {
    const currentRequest = permissionRequest()
    const currentSessionId = sessionId()
    if (!currentRequest || !currentSessionId) return

    setPermissionRequest(null)
    await props.client.resolvePermission(currentSessionId, currentRequest.requestId, allow)
    setIsStreaming(true)
  }

  function handlePromptSubmit(text: string) {
    if (isStreaming()) {
      abortController?.abort()
    }
    sendMessage(text)
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Tab") {
      event.preventDefault()
      const agents = availableAgents()
      const currentIndex = agents.indexOf(currentAgent())
      const nextIndex = (currentIndex + 1) % agents.length
      setCurrentAgent(agents[nextIndex])
    }
    if (event.key === "Escape" && permissionRequest() !== null) {
      handlePermissionResponse(false)
    }
  }

  const agentColor = () => agentColors[currentAgent()] ?? theme.primary

  return (
    <box flexGrow={1} minHeight={0} onKeyDown={handleKeyDown}>
      <box flexGrow={1} minHeight={0} paddingBottom={1} paddingLeft={2} paddingRight={2}>
        <box flexGrow={1} flexDirection="column">
          <scrollbox
            ref={(reference: any) => (scrollableContainer = reference)}
            flexGrow={1}
            stickyStart="bottom"
            stickyScroll={true}
          >
            <For each={messages()}>
              {(message) => <ChatMessageComponent message={message} />}
            </For>

            <Show when={isStreaming()}>
              <box flexDirection="row" gap={1} paddingTop={1}>
                <Spinner color={theme.primary}>
                  <Show when={streamingText()}>
                    <text fg={theme.textMuted}>
                      {streamingText().slice(0, 80)}
                      <Show when={streamingText().length > 80}>...</Show>
                    </text>
                  </Show>
                </Spinner>
              </box>
              <Show when={streamingToolCalls().length > 0}>
                <For each={streamingToolCalls()}>
                  {(toolCall, index) => (
                    <box flexDirection="row" gap={1} paddingLeft={2}>
                      <text fg={theme.primary}>●</text>
                      <text fg={theme.text}>{toolCall.name}</text>
                      <Show when={toolCall.justification}>
                        <text fg={theme.textMuted}>— {toolCall.justification}</text>
                      </Show>
                    </box>
                  )}
                </For>
              </Show>
            </Show>

            <Show when={connectionError()}>
              <text fg={theme.error}>{connectionError()}</text>
            </Show>
          </scrollbox>

          <PermissionDialog
            request={permissionRequest()}
            onRespond={handlePermissionResponse}
          />

          <Prompt
            onSubmit={handlePromptSubmit}
            disabled={false}
            placeholder={isStreaming() ? "Press Enter to interrupt..." : "Type a message..."}
          />
        </box>
      </box>
      <StatusBar
        serverUrl={props.serverUrl}
        agent={currentAgent()}
        agentColorOverride={agentColor()}
      />
    </box>
  )
}
