import { For } from "solid-js"
import { logo } from "../logo"

export function Logo(props: { foregroundColor?: string; mutedColor?: string }) {
  const foreground = () => props.foregroundColor ?? "#e4e4e7"
  const muted = () => props.mutedColor ?? "#71717a"

  return (
    <box flexDirection="column" alignItems="center">
      <For each={logo.left}>
        {(line, index) => (
          <box flexDirection="row" gap={1}>
            <text fg={muted()}>{line}</text>
            <text fg={foreground()}>{logo.right[index()]}</text>
          </box>
        )}
      </For>
    </box>
  )
}
