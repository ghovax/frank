import { createContext, useContext, type ParentProps } from "solid-js"

export type Theme = {
  background: string
  backgroundPanel: string
  backgroundElement: string
  text: string
  textMuted: string
  primary: string
  border: string
  borderActive: string
  success: string
  warning: string
  error: string
}

export const darkTheme: Theme = {
  background: "#0a0a0b",
  backgroundPanel: "#121214",
  backgroundElement: "#1a1a1e",
  text: "#e4e4e7",
  textMuted: "#71717a",
  primary: "#8b5cf6",
  border: "#27272a",
  borderActive: "#52525b",
  success: "#22c55e",
  warning: "#f59e0b",
  error: "#ef4444",
}

export const agentColors: Record<string, string> = {
  main: "#06b6d4",
  code: "#eab308",
  explore: "#22c55e",
}

const ThemeContext = createContext<{ theme: Theme }>()

export function ThemeProvider(props: ParentProps<{ theme: Theme }>) {
  return (
    <ThemeContext.Provider value={{ theme: props.theme }}>
      {props.children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  const context = useContext(ThemeContext)
  if (!context) throw new Error("useTheme must be used within a ThemeProvider")
  return context.theme
}
