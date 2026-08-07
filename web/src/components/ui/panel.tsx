"use client";

// Shared building blocks for the floating side panels, which are all the same card.

import { Box, EmptyState, Flex, IconButton, Text, VStack, type BoxProps, type FlexProps } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuX } from "react-icons/lu";
import { useScrollEdgeFade } from "@/lib/scroll-fade";

// The single height every top strip shares, so all their titles sit on one line.
export const TOP_BAR_HEIGHT = "3rem";

// The panel surface: a filled card that fills its tile, with the standard border and elevation.
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

// The panel's top bar: a leading icon, the title, any inline actions, and an optional close button.
export function PanelHeader({
  icon,
  title,
  onClose,
  closeLabel = "Collapse panel",
  children,
  ...rest
}: {
  icon?: ReactNode;
  // Optional, since a panel whose whole strip is a custom control supplies that instead of a title.
  title?: ReactNode;
  onClose?: () => void;
  closeLabel?: string;
  children?: ReactNode;
} & FlexProps) {
  return (
    // Default strip padding, matching the body's content edge and insetting a plain title a little more.
    <Flex align="center" gap={2} pl={3} pr={2} h={TOP_BAR_HEIGHT} flexShrink={0} {...rest}>
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

// The panel's scrolling content area, filling the height below the header with the standard inset.
export function PanelBody({ children, ...rest }: BoxProps) {
  const { containerRef, onScroll, fade } = useScrollEdgeFade();
  return (
    <Box ref={containerRef} onScroll={onScroll} flex={1} minH={0} overflowY="auto" px={2} pb={2} pt={0} css={fade} {...rest}>
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
