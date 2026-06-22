import { createSignal, onMount, onCleanup } from "solid-js"
import { useTheme } from "../theme"

export function StatusBar(props: { serverUrl: string; agent?: string; agentColorOverride?: string }) {
  const [isConnected, setIsConnected] = createSignal(false)
  const theme = useTheme()

  const agentLabel = () => props.agent ?? "main"
  const agentColor = () => props.agentColorOverride ?? theme.primary

  let healthCheckInterval: ReturnType<typeof setInterval> | undefined

  onMount(() => {
    checkServerHealth()
    healthCheckInterval = setInterval(checkServerHealth, 10000)
  })

  onCleanup(() => {
    if (healthCheckInterval) clearInterval(healthCheckInterval)
  })

  async function checkServerHealth() {
    try {
      const response = await fetch(`${props.serverUrl}/agents`, { signal: AbortSignal.timeout(2000) })
      setIsConnected(response.ok)
    } catch {
      setIsConnected(false)
    }
  }

  return (
    <box
      flexDirection="row"
      justifyContent="space-between"
      gap={1}
      flexShrink={0}
      paddingLeft={1}
      paddingRight={1}
      height={1}
    >
      <box flexDirection="row" gap={1}>
        <text fg={isConnected() ? theme.success : theme.error}>●</text>
        <text fg={agentColor()} bold>
          {agentLabel()}
        </text>
      </box>
      <text fg={theme.textMuted}>{props.serverUrl}</text>
    </box>
  )
}
