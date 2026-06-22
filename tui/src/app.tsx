import { render as renderTui, useRenderer, useTerminalDimensions } from "@opentui/solid"
import { createDefaultOpenTuiKeymap } from "@opentui/keymap/opentui"
import { KeymapProvider, useBindings } from "@opentui/keymap/solid"
import { createSignal, Switch, Match, ErrorBoundary, batch, onCleanup } from "solid-js"
import { createCliRenderer } from "@opentui/core"
import { Home } from "./routes/home"
import { Session } from "./routes/session"
import { AgenticHarnessClient } from "./client"
import { ThemeProvider, darkTheme, useTheme } from "./theme"

export type TuiInput = {
  url: string
  directory?: string
}

export async function run(input: TuiInput) {
  const renderer = await createCliRenderer({
    targetFps: 30,
    exitOnCtrlC: true,
    useMouse: true,
  })

  renderer.start()

  const keymap = createDefaultOpenTuiKeymap(renderer)

  await renderTui(() => (
    <KeymapProvider keymap={keymap}>
      <ThemeProvider theme={darkTheme}>
        <ErrorBoundary fallback={(error) => <text fg="#ef4444">Error: {String(error)}</text>}>
          <TuiApplication input={input} />
        </ErrorBoundary>
      </ThemeProvider>
    </KeymapProvider>
  ), renderer)

  await new Promise<void>((resolve) => {
    renderer.once("destroy", () => resolve())
  })

  renderer.destroy()
}

type ApplicationRoute =
  | { type: "home" }
  | { type: "session"; client: AgenticHarnessClient; message: string; agent: string }

function TuiApplication(props: { input: TuiInput }) {
  const renderer = useRenderer()
  const terminalDimensions = useTerminalDimensions()
  const theme = useTheme()

  const [currentRoute, setCurrentRoute] = createSignal<ApplicationRoute>({ type: "home" })

  useBindings(() => ({
    commands: [
      {
        name: "quit",
        run() { renderer.destroy() },
      },
    ],
    bindings: [{ key: "q", cmd: "quit" }],
  }))

  function handleNavigateToSession(client: AgenticHarnessClient, message: string, agent: string) {
    batch(() => {
      setCurrentRoute({ type: "session", client, message, agent })
    })
  }

  function handleNavigateHome() {
    setCurrentRoute({ type: "home" })
  }

  return (
    <box
      width={terminalDimensions().width}
      height={terminalDimensions().height}
      flexDirection="column"
      backgroundColor={theme.background}
    >
      <Switch>
        <Match when={currentRoute().type === "home"}>
          <Home
            serverUrl={props.input.url}
            onStart={handleNavigateToSession}
          />
        </Match>
        <Match when={currentRoute().type === "session"}>
          <Session
            serverUrl={props.input.url}
            client={(currentRoute() as Extract<ApplicationRoute, { type: "session" }>).client}
            initialMessage={(currentRoute() as Extract<ApplicationRoute, { type: "session" }>).message}
            initialAgent={(currentRoute() as Extract<ApplicationRoute, { type: "session" }>).agent}
            onReturnHome={handleNavigateHome}
          />
        </Match>
      </Switch>
    </box>
  )
}
