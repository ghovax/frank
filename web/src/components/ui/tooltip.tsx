import { Tooltip as ChakraTooltip, Portal } from "@chakra-ui/react"
import * as React from "react"

// The card styling for a "rich" tooltip — a small floating panel (padding, solid bg,
// border, drop shadow) used wherever a tooltip carries structured content (fielded rows,
// a title + detail) rather than a one-line hint. Set once here so every rich tooltip
// matches, via the `rich` prop, instead of repeating this object at each call site.
const RICH_CONTENT_PROPS = {
  p: 3,
  bg: "bg",
  color: "fg",
  fontSize: "xs",
  lineHeight: "1.6",
  boxShadow: "lg",
  border: "1px solid",
  borderColor: "border",
  // Bound the card and keep any long/unbreakable value inside it: without a max
  // width a `whiteSpace="nowrap"` content box grows to its widest line and any
  // untruncated field spills past the border. `maxW` caps it, `overflow="hidden"`
  // clips the overrun, and `overflowWrap` lets long tokens break instead of pushing.
  maxW: "20rem",
  overflow: "hidden",
  overflowWrap: "anywhere",
} as const

export interface TooltipProps extends ChakraTooltip.RootProps {
  showArrow?: boolean
  portalled?: boolean
  portalRef?: React.RefObject<HTMLElement | null>
  content: React.ReactNode
  contentProps?: ChakraTooltip.ContentProps
  // Render as a rich card (padded, bordered, shadowed) for structured content. Any
  // `contentProps` still override the rich defaults.
  rich?: boolean
  disabled?: boolean
}

export const Tooltip = React.forwardRef<HTMLDivElement, TooltipProps>(
  function Tooltip(props, ref) {
    const {
      showArrow,
      children,
      disabled,
      portalled = true,
      content,
      contentProps,
      rich,
      portalRef,
      ...rest
    } = props

    if (disabled) return children

    return (
      <ChakraTooltip.Root {...rest}>
        <ChakraTooltip.Trigger asChild>{children}</ChakraTooltip.Trigger>
        <Portal disabled={!portalled} container={portalRef}>
          <ChakraTooltip.Positioner>
            <ChakraTooltip.Content ref={ref} borderRadius="md" {...(rich ? RICH_CONTENT_PROPS : {})} {...contentProps}>
              {showArrow && (
                <ChakraTooltip.Arrow>
                  <ChakraTooltip.ArrowTip />
                </ChakraTooltip.Arrow>
              )}
              {content}
            </ChakraTooltip.Content>
          </ChakraTooltip.Positioner>
        </Portal>
      </ChakraTooltip.Root>
    )
  },
)
