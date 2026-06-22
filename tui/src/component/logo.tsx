import { For } from "solid-js"
import { useTheme } from "../theme"
import { logo } from "../logo"

export function Logo() {
  const theme = useTheme()
  return (
    <box flexDirection="column" alignItems="center">
      <For each={logo.left}>
        {(line, index) => (
          <box flexDirection="row" gap={1}>
            <text fg={theme.textMuted}>{line}</text>
            <text fg={theme.text}>{logo.right[index()]}</text>
          </box>
        )}
      </For>
    </box>
  )
}
