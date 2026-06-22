import { For, Show } from "solid-js"
import { useTheme, agentColors } from "../theme"
import type { ChatMessage } from "../client"

export function ChatMessage(props: { message: ChatMessage }) {
  const theme = useTheme()
  const message = () => props.message

  const agentColor = () => agentColors[message().agent ?? "main"] ?? theme.primary

  return (
    <box flexDirection="column" gap={0} paddingY={0}>
      <Show when={message().role === "user"}>
        <box flexDirection="row" gap={1} paddingTop={1}>
          <text fg={theme.textMuted} bold>
            User
          </text>
          <text fg={theme.textMuted}>{message().timestamp}</text>
        </box>
        <box backgroundColor="#18181b" paddingX={1}>
          <text fg={theme.text}>{message().text}</text>
        </box>
      </Show>

      <Show when={message().role === "assistant"}>
        <box flexDirection="row" gap={1} paddingTop={1}>
          <text fg={agentColor()} bold>
            {message().agent ?? "Assistant"}
          </text>
          <text fg={theme.textMuted}>{message().timestamp}</text>
        </box>

        <Show when={message().toolCalls && message().toolCalls!.length > 0}>
          <For each={message().toolCalls}>
            {(toolCall) => (
              <box flexDirection="column" paddingLeft={2} gap={0}>
                <box flexDirection="row" gap={1}>
                  <text fg={theme.primary}>Tool:</text>
                  <text fg={theme.text}>{toolCall.name}</text>
                </box>
                <Show when={toolCall.justification}>
                  <text fg={theme.textMuted} paddingLeft={2}>
                    {toolCall.justification}
                  </text>
                </Show>
                <Show when={toolCall.result}>
                  <text fg={theme.textMuted} paddingLeft={2} wrapMode="none">
                    Result: {toolCall.result!.slice(0, 300)}
                  </text>
                </Show>
              </box>
            )}
          </For>
        </Show>

        <Show when={message().text}>
          <box paddingX={1}>
            <text fg={theme.text}>{message().text}</text>
          </box>
        </Show>

        <Show when={message().error}>
          <text fg={theme.error}>{message().error}</text>
        </Show>
      </Show>

      <text fg={theme.border}>{"─".repeat(40)}</text>
    </box>
  )
}
