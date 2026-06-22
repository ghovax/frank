import type { JSX } from "@opentui/solid"
import "opentui-spinner/solid"

export const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

export function Spinner(props: { children?: JSX.Element; color?: string }) {
  const spinnerColor = () => props.color ?? "#71717a"
  return (
    <box flexDirection="row" gap={1}>
      <spinner frames={SPINNER_FRAMES} interval={80} color={spinnerColor()} />
      {props.children}
    </box>
  )
}
