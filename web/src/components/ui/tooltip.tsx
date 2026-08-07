import { Tooltip as ChakraTooltip, Portal } from "@chakra-ui/react";
import * as React from "react";

import { useCoarsePointer } from "@/lib/pointer";

// How long a tapped tooltip stays up: a finger has no "away" to dismiss it with.
const TAP_DISMISS_MILLISECONDS = 6000;

// The card styling for a rich tooltip, set once so every one of them reads the same.
const RICH_CONTENT_PROPS = {
  p: 3,
  bg: "bg",
  color: "fg",
  fontSize: "xs",
  lineHeight: "1.6",
  boxShadow: "lg",
  border: "1px solid",
  borderColor: "border",
  // Bound the card, or a nowrap content box grows to its widest line and spills past the border.
  maxW: "20rem",
  overflow: "hidden",
  overflowWrap: "anywhere",
} as const;

export interface TooltipProps extends ChakraTooltip.RootProps {
  showArrow?: boolean;
  portalled?: boolean;
  portalRef?: React.RefObject<HTMLElement | null>;
  content: React.ReactNode;
  contentProps?: ChakraTooltip.ContentProps;
  // Render as a rich card for structured content; `contentProps` still override the defaults.
  rich?: boolean;
  disabled?: boolean;
}

export const Tooltip = React.forwardRef<HTMLDivElement, TooltipProps>(function Tooltip(props, ref) {
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
  } = props;

  // Tap to open, since the tooltip machine opens on hover and a touch device has none.
  const coarsePointer = useCoarsePointer();
  const [tapped, setTapped] = React.useState(false);

  React.useEffect(() => {
    if (!tapped) return;
    const timer = window.setTimeout(() => setTapped(false), TAP_DISMISS_MILLISECONDS);
    return () => window.clearTimeout(timer);
  }, [tapped]);

  if (disabled) return children;

  const touch = coarsePointer
    ? {
        open: tapped,
        onOpenChange: (event: { open: boolean }) => setTapped(event.open),
      }
    : {};

  return (
    // Two defaults overturned, both because this app streams into a pane that scrolls itself.
    <ChakraTooltip.Root closeOnScroll={false} interactive {...rest} {...touch}>
      <ChakraTooltip.Trigger
        asChild
        onClick={coarsePointer ? () => setTapped((shown) => !shown) : undefined}
      >
        {children}
      </ChakraTooltip.Trigger>
      <Portal disabled={!portalled} container={portalRef}>
        <ChakraTooltip.Positioner>
          <ChakraTooltip.Content
            ref={ref}
            borderRadius="md"
            {...(rich ? RICH_CONTENT_PROPS : {})}
            {...contentProps}
          >
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
  );
});
