import { createContext, useContext, type ParentProps } from "solid-js"
import { RGBA } from "@opentui/core"

export const agentColors: Record<string, string> = {
  main: "#06b6d4",
  code: "#eab308",
  explore: "#22c55e",
}

export type Theme = {
  primary: RGBA
  secondary: RGBA
  accent: RGBA
  error: RGBA
  warning: RGBA
  success: RGBA
  info: RGBA
  text: RGBA
  textMuted: RGBA
  background: RGBA
  backgroundPanel: RGBA
  backgroundElement: RGBA
  backgroundMenu: RGBA
  border: RGBA
  borderActive: RGBA
  borderSubtle: RGBA
  diffAdded: RGBA
  diffRemoved: RGBA
  diffContext: RGBA
  diffHunkHeader: RGBA
  diffHighlightAdded: RGBA
  diffHighlightRemoved: RGBA
  diffAddedBg: RGBA
  diffRemovedBg: RGBA
  diffContextBg: RGBA
  diffLineNumber: RGBA
  diffAddedLineNumberBg: RGBA
  diffRemovedLineNumberBg: RGBA
  markdownText: RGBA
  markdownHeading: RGBA
  markdownLink: RGBA
  markdownLinkText: RGBA
  markdownCode: RGBA
  markdownBlockQuote: RGBA
  markdownEmph: RGBA
  markdownStrong: RGBA
  markdownHorizontalRule: RGBA
  markdownListItem: RGBA
  markdownListEnumeration: RGBA
  markdownImage: RGBA
  markdownImageText: RGBA
  markdownCodeBlock: RGBA
  syntaxComment: RGBA
  syntaxKeyword: RGBA
  syntaxFunction: RGBA
  syntaxVariable: RGBA
  syntaxString: RGBA
  syntaxNumber: RGBA
  syntaxType: RGBA
  syntaxOperator: RGBA
  syntaxPunctuation: RGBA
  selectedListItemText: RGBA
  thinkingOpacity: number
  _hasSelectedListItemText: boolean
}

function hex(hex: string): RGBA {
  return RGBA.fromHex(hex)
}

export const darkTheme: Theme = {
  primary: hex("#fab283"),
  secondary: hex("#5c9cf5"),
  accent: hex("#9d7cd8"),
  error: hex("#e06c75"),
  warning: hex("#f5a742"),
  success: hex("#7fd88f"),
  info: hex("#56b6c2"),
  text: hex("#eeeeee"),
  textMuted: hex("#808080"),
  background: hex("#0a0a0a"),
  backgroundPanel: hex("#141414"),
  backgroundElement: hex("#1e1e1e"),
  backgroundMenu: hex("#1e1e1e"),
  border: hex("#484848"),
  borderActive: hex("#606060"),
  borderSubtle: hex("#3c3c3c"),
  diffAdded: hex("#4fd6be"),
  diffRemoved: hex("#c53b53"),
  diffContext: hex("#828bb8"),
  diffHunkHeader: hex("#828bb8"),
  diffHighlightAdded: hex("#b8db87"),
  diffHighlightRemoved: hex("#e26a75"),
  diffAddedBg: hex("#20303b"),
  diffRemovedBg: hex("#37222c"),
  diffContextBg: hex("#141414"),
  diffLineNumber: hex("#8f8f8f"),
  diffAddedLineNumberBg: hex("#1b2b34"),
  diffRemovedLineNumberBg: hex("#2d1f26"),
  markdownText: hex("#eeeeee"),
  markdownHeading: hex("#9d7cd8"),
  markdownLink: hex("#fab283"),
  markdownLinkText: hex("#56b6c2"),
  markdownCode: hex("#7fd88f"),
  markdownBlockQuote: hex("#e5c07b"),
  markdownEmph: hex("#e5c07b"),
  markdownStrong: hex("#f5a742"),
  markdownHorizontalRule: hex("#808080"),
  markdownListItem: hex("#fab283"),
  markdownListEnumeration: hex("#56b6c2"),
  markdownImage: hex("#fab283"),
  markdownImageText: hex("#56b6c2"),
  markdownCodeBlock: hex("#eeeeee"),
  syntaxComment: hex("#808080"),
  syntaxKeyword: hex("#9d7cd8"),
  syntaxFunction: hex("#fab283"),
  syntaxVariable: hex("#e06c75"),
  syntaxString: hex("#7fd88f"),
  syntaxNumber: hex("#f5a742"),
  syntaxType: hex("#e5c07b"),
  syntaxOperator: hex("#56b6c2"),
  syntaxPunctuation: hex("#eeeeee"),
  selectedListItemText: hex("#eeeeee"),
  thinkingOpacity: 0.6,
  _hasSelectedListItemText: false,
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
