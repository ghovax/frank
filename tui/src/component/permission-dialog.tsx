import { Show } from "solid-js"
import { useTheme } from "../theme"

export type PermissionRequest = {
  requestId: string
  command: string
  justification: string
  risk: string
}

export function PermissionDialog(props: {
  request: PermissionRequest | null
  onRespond: (allow: boolean) => void
}) {
  const theme = useTheme()

  return (
    <Show when={props.request}>
      {(request) => (
        <box
          flexDirection="column"
          gap={1}
          padding={1}
          backgroundColor={theme.backgroundPanel}
          border={["left"]}
          borderColor={theme.warning}
        >
          <text fg={theme.warning} bold>
            Permission Request
          </text>
          <Show when={request().command}>
            <text fg={theme.text}>{request().command}</text>
          </Show>
          <Show when={request().justification}>
            <text fg={theme.textMuted}>Why: {request().justification}</text>
          </Show>
          <Show when={request().risk}>
            <text fg={theme.error}>Risk: {request().risk}</text>
          </Show>
          <box flexDirection="row" gap={2}>
            <text
              fg={theme.success}
              onMouseUp={() => props.onRespond(true)}
              onClick={() => props.onRespond(true)}
            >
              [Y] Allow
            </text>
            <text
              fg={theme.error}
              onMouseUp={() => props.onRespond(false)}
              onClick={() => props.onRespond(false)}
            >
              [N] Deny
            </text>
          </box>
        </box>
      )}
    </Show>
  )
}
