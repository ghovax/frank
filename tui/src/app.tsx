import { render, useTerminalDimensions } from "@opentui/solid"
import { createDefaultOpenTuiKeymap } from "@opentui/keymap/opentui"
import {
  registerCommaBindings,
  registerEscapeClearsPendingSequence,
  registerBackspacePopsPendingSequence,
} from "@opentui/keymap/addons/opentui"
import { KeymapProvider, useBindings } from "@opentui/keymap/solid"
import { createSignal, Switch, Match, ErrorBoundary, batch, onCleanup, createEffect } from "solid-js"
import { createCliRenderer } from "@opentui/core"
import { Home } from "./routes/home"
import { Session } from "./routes/session"
import { AgenticHarnessClient } from "./client"
import { ThemeProvider, useTheme, darkTheme } from "./theme"

export type TuiInput = {
  url: string
  directory?: string
}

export async function run(input: TuiInput) {
  const renderer = await createCliRenderer({
    externalOutputMode: "passthrough",
    targetFps: 30,
    gatherStats: false,
    exitOnCtrlC: false,
    useKittyKeyboard: {},
    autoFocus: false,
    openConsoleOnError: false,
    useMouse: true,
  })

  renderer.start()

  const keymap = createDefaultOpenTuiKeymap(renderer)
  registerCommaBindings(keymap)
  registerEscapeClearsPendingSequence(keymap)
  registerBackspacePopsPendingSequence(keymap)

  const shutdown = new Promise<void>((resolve) => {
    renderer.once("destroy", () => resolve())
  })

  await render(() => (
    <KeymapProvider keymap={keymap}>
      <ThemeProvider theme={darkTheme}>
        <ErrorBoundary fallback={(error) => <text fg="#ef4444">Error: {String(error)}</text>}>
          <TuiApplication input={input} renderer={renderer} />
        </ErrorBoundary>
      </ThemeProvider>
    </KeymapProvider>
  ), renderer)

  await shutdown
  renderer.destroy()
}

type ApplicationRoute =
  | { type: "home" }
  | { type: "session"; client: AgenticHarnessClient; message: string; agent: string }

function TuiApplication(props: { input: TuiInput; renderer: any }) {
  const terminalDimensions = useTerminalDimensions()
  const theme = useTheme()

  const [currentRoute, setCurrentRoute] = createSignal<ApplicationRoute>({ type: "home" })

  useBindings(() => ({
    commands: [
      {
        name: "quit",
        run() { props.renderer.destroy() },
      },
    ],
    bindings: [{ key: "q", cmd: "quit" }],
  }))

  useBindings(() => ({
    bindings: [{ key: "ctrl+c", cmd: "quit" }],
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
