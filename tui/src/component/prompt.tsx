import { createSignal } from "solid-js"
import { useTheme } from "../theme"

export function Prompt(props: {
  onSubmit: (text: string) => void
  disabled?: boolean
  placeholder?: string
}) {
  const [value, setValue] = createSignal("")
  const theme = useTheme()

  const handleKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      const textContent = value().trim()
      if (textContent && !props.disabled) {
        props.onSubmit(textContent)
        setValue("")
      }
    }
    if (event.key === "Escape") {
      setValue("")
    }
  }

  return (
    <box flexDirection="row" gap={1} alignItems="center" width="100%">
      <text fg={theme.textMuted}>{">"}</text>
      <input
        value={value()}
        onInput={(event: Event) => setValue((event.target as HTMLInputElement).value)}
        onKeyDown={handleKeyDown}
        placeholder={props.placeholder ?? "Type a message..."}
        fg={theme.text}
        bg={theme.backgroundPanel}
        width="100%"
        height={1}
      />
    </box>
  )
}
