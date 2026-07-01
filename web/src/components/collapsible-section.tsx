"use client";

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { Children, useState, type ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";

interface CollapsibleSectionProps {
  title: string;
  subtitle?: string;
  count?: number;
  icon?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  initialVisibleCount?: number;
}

export function CollapsibleSection({
  title,
  subtitle,
  count,
  icon,
  children,
  defaultOpen = true,
  initialVisibleCount = 5,
}: CollapsibleSectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [showAll, setShowAll] = useState(false);
  const items = Children.toArray(children).filter(Boolean);
  const visibleItems = showAll ? items : items.slice(0, initialVisibleCount);
  const hiddenCount = Math.max(0, items.length - visibleItems.length);

  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      <Flex
        as="button"
        align="center"
        gap={1.5}
        w="100%"
        px={2}
        py={1.5}
        minH="8"
        textAlign="left"
        cursor="pointer"
        color="fg"
        _hover={{ bg: "bg.muted" }}
        onClick={() => setOpen((current) => !current)}
        title={subtitle}
      >
        <Box color="fg.muted" display="flex" alignItems="center" flexShrink={0}>
          {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
        </Box>
        {icon ? (
          <Box color="fg.muted" display="flex" alignItems="center" flexShrink={0}>
            {icon}
          </Box>
        ) : null}
        <Text fontSize="xs" fontWeight="semibold" truncate flex={1} minW={0}>
          {title}
        </Text>
        {typeof count === "number" ? (
          <Text fontSize="xs" color="fg.subtle" flexShrink={0}>
            {count}
          </Text>
        ) : null}
      </Flex>
      {open ? (
        <Flex direction="column" gap={1} px={1.5} py={1.5} borderTop="1px solid" borderColor="border" bg="bg">
          {visibleItems}
          {hiddenCount > 0 ? (
            <Button
              size="xs"
              variant="ghost"
              borderRadius="sm"
              fontSize="xs"
              h="26px"
              onClick={() => setShowAll(true)}
            >
              Show {hiddenCount} more
            </Button>
          ) : null}
        </Flex>
      ) : null}
    </Box>
  );
}
