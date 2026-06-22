import { createSignal, createEffect } from "solid-js"
import { useTerminalDimensions } from "@opentui/solid"
import { Logo } from "../component/logo"
import { Prompt } from "../component/prompt"
import { StatusBar } from "../component/status-bar"
import { AgenticHarnessClient } from "../client"
import { useTheme, agentColors } from "../theme"

export function Home(props: {
  serverUrl: string
  onStart: (client: AgenticHarnessClient, message: string, agent: string) => void
}) {
  const theme = useTheme()
  const terminalDimensions = useTerminalDimensions()
  const agenticHarnessClient = new AgenticHarnessClient(props.serverUrl)

  const [availableAgents, setAvailableAgents] = createSignal<string[]>(["main"])
  const [currentAgent, setCurrentAgent] = createSignal("main")

  const promptMaxWidth = () => Math.max(75, Math.floor(terminalDimensions().width * 0.7))

  createEffect(async () => {
    try {
      const agentsFromServer = await agenticHarnessClient.listAgents()
      if (agentsFromServer.length > 0) setAvailableAgents(agentsFromServer)
    } catch {
      // fall back to default agent
    }
  })

  const handlePromptSubmit = (text: string) => {
    props.onStart(agenticHarnessClient, text, currentAgent())
  }

  const handleAgentSwitchKeyPress = (event: KeyboardEvent) => {
    if (event.key === "Tab") {
      event.preventDefault()
      const agents = availableAgents()
      const currentIndex = agents.indexOf(currentAgent())
      const nextIndex = (currentIndex + 1) % agents.length
      setCurrentAgent(agents[nextIndex])
    }
  }

  const agentColor = () => agentColors[currentAgent()] ?? theme.primary

  return (
    <box flexGrow={1} alignItems="center" onKeyDown={handleAgentSwitchKeyPress}>
      <box flexGrow={1} minHeight={0} />
      <box height={4} minHeight={0} flexShrink={1} />
      <box flexShrink={0}>
        <Logo />
      </box>
      <box height={1} minHeight={0} flexShrink={1} />
      <box
        width="100%"
        maxWidth={promptMaxWidth()}
        paddingTop={1}
        flexShrink={0}
      >
        <Prompt onSubmit={handlePromptSubmit} placeholder="Ask anything..." />
      </box>
      <box flexGrow={1} minHeight={0} />
      <StatusBar
        serverUrl={props.serverUrl}
        agent={currentAgent()}
        agentColorOverride={agentColor()}
      />
    </box>
  )
}
