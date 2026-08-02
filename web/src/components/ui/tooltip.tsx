import { Tooltip as ChakraTooltip, Portal } from "@chakra-ui/react"
import * as React from "react"

import { useCoarsePointer } from "@/lib/pointer"

// How long a tapped tooltip stays up before dismissing itself.
//
// A tooltip has no close button, because on a pointer it never needs one — moving away is the
// dismissal. A finger has no "away", so one opened by tapping would sit over the interface until
// something else was tapped. Long enough to read a line or two of numbers, short enough that it
// is gone before it is in the way.
const TAP_DISMISS_MILLISECONDS = 6000

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

    // Tap to open, where there is nothing to hover with.
    //
    // The tooltip machine opens on hover and has no click to open with, so on a touch device
    // every tooltip in this app was simply unreachable — and some of them are the only place a
    // number appears. Held open here instead, toggled by tapping whatever the tooltip is on.
    //
    // Composed rather than replacing: `asChild` merges these props with the child's through
    // zag's `mergeProps`, which calls both handlers for any `on*` key. So a tooltip on a button
    // still presses the button.
    const coarsePointer = useCoarsePointer()
    const [tapped, setTapped] = React.useState(false)

    React.useEffect(() => {
      if (!tapped) return
      const timer = window.setTimeout(() => setTapped(false), TAP_DISMISS_MILLISECONDS)
      return () => window.clearTimeout(timer)
    }, [tapped])

    if (disabled) return children

    const touch = coarsePointer
      ? {
          open: tapped,
          onOpenChange: (event: { open: boolean }) => setTapped(event.open),
        }
      : {}

    return (
      <ChakraTooltip.Root {...rest} {...touch}>
        <ChakraTooltip.Trigger
          asChild
          onClick={coarsePointer ? () => setTapped((shown) => !shown) : undefined}
        >
          {children}
        </ChakraTooltip.Trigger>
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
