"use client";

// Shared building blocks for the floating side panels (Agents, Artifacts, Terminal/
// Background, and any future one). Every panel is the same card with the same header and
// the same empty state, so those live here once instead of being re-spelled per panel —
// keeping each panel's own file down to just its content.

import { Box, EmptyState, Flex, IconButton, Text, VStack, type BoxProps, type FlexProps } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuX } from "react-icons/lu";
import { useScrollEdgeFade } from "@/lib/scroll-fade";

// The panel surface: a filled card that fills its tile (PanelTiles owns size + placement)
// with the standard border + soft `panel` elevation. Extra props pass through so a panel
// can add e.g. `position="relative"` or hover handlers.
export function PanelCard({ children, ...rest }: FlexProps) {
  return (
    <Flex
      direction="column"
      h="full"
      w="full"
      minW={0}
      minH={0}
      bg="bg.panel"
      borderRadius="md"
      borderWidth="1px"
      borderColor="border.muted"
      boxShadow="panel"
      overflow="hidden"
      {...rest}
    >
      {children}
    </Flex>
  );
}

// The panel's top bar: a muted leading icon, the semibold title, any inline actions
// (passed as children — e.g. a segmented toggle), and an optional trailing close button.
export function PanelHeader({
  icon,
  title,
  onClose,
  closeLabel = "Collapse panel",
  children,
  ...rest
}: {
  icon?: ReactNode;
  // Optional: a panel whose whole strip is a custom control (e.g. the sessions sidebar's
  // project switcher) passes no title and supplies the control as `children` instead.
  title?: ReactNode;
  onClose?: () => void;
  closeLabel?: string;
  children?: ReactNode;
} & FlexProps) {
  return (
    // Default strip padding: pr={2} matches the body's content edge; pl={3} insets a plain
    // title slightly more. A panel whose whole strip is a full-width control (the sessions
    // sidebar) overrides `pl={2}` so its button lines up with the body rows below it — `...rest`
    // lets that win.
    <Flex align="center" gap={2} pl={3} pr={2} py={2} flexShrink={0} {...rest}>
      {icon ? <Box color="fg.muted">{icon}</Box> : null}
      {title ? (
        <Text textStyle="panelTitle" flex={1} minW={0} truncate>
          {title}
        </Text>
      ) : null}
      {children}
      {onClose ? (
        <IconButton aria-label={closeLabel} variant="ghost" onClick={onClose}>
          <LuX size={14} />
        </IconButton>
      ) : null}
    </Flex>
  );
}

// The panel's scrolling content area: fills the remaining height below the header, scrolls
// on overflow, and carries the standard inset padding. Content softly fades at whichever
// edges hide more of it (under the header when scrolled down, above the panel's bottom
// while more is below the fold). Extra props pass through.
export function PanelBody({ children, ...rest }: BoxProps) {
  const { containerRef, onScroll, fade } = useScrollEdgeFade();
  return (
    <Box ref={containerRef} onScroll={onScroll} flex={1} minH={0} overflowY="auto" px={2} py={2} css={fade} {...rest}>
      {children}
    </Box>
  );
}

// The compact empty state shown in a panel's scroll area when it has nothing yet.
export function PanelEmptyState({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: ReactNode;
  description?: ReactNode;
}) {
  return (
    <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2} pt={4} pb={12}>
      <EmptyState.Root size="sm">
        <EmptyState.Content>
          <EmptyState.Indicator>{icon}</EmptyState.Indicator>
          <VStack gap={1}>
            <EmptyState.Title fontSize="sm">{title}</EmptyState.Title>
            {description ? (
              <EmptyState.Description fontSize="xs">{description}</EmptyState.Description>
            ) : null}
          </VStack>
        </EmptyState.Content>
      </EmptyState.Root>
    </Flex>
  );
}
