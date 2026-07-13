"use client";

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { Children, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { AnimatePresence, motion } from "motion/react";
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
  const t = useTranslations("CollapsibleSection");
  const [open, setOpen] = useState(defaultOpen);
  const [showAll, setShowAll] = useState(false);
  const items = Children.toArray(children).filter(Boolean);
  const hasMoreItems = items.length > initialVisibleCount;
  const visibleItems = showAll ? items : items.slice(0, initialVisibleCount);
  const hiddenCount = Math.max(0, items.length - visibleItems.length);

  return (
    <Box borderRadius="md" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
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
        <Text textStyle="sectionLabel" truncate flex={1} minW={0}>
          {title}
        </Text>
        {typeof count === "number" ? (
          <Text textStyle="fieldLabel" color="fg.subtle" flexShrink={0} pr={1}>
            {t("sessionCount", { count })}
          </Text>
        ) : null}
      </Flex>
      {open ? (
        <Flex direction="column" gap={1} px={1.5} pt={1.5} pb={hasMoreItems ? 1.5 : 0.5} borderTop="1px solid" borderColor="border" bg="bg" overflow="hidden">
          <AnimatePresence initial={false}>
            {visibleItems.map((item, index) => (
              <motion.div
                key={(item as React.ReactElement)?.key ?? index}
                initial={{ opacity: 0, height: 0, marginBottom: 0 }}
                animate={{ opacity: 1, height: "auto", marginBottom: 4 }}
                exit={{ opacity: 0, height: 0, marginBottom: 0 }}
                transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
                style={{ overflow: "hidden" }}
              >
                {item}
              </motion.div>
            ))}
          </AnimatePresence>
          {hasMoreItems ? (
            <Button
              variant="ghost"
              h={8}
              onClick={() => setShowAll((current) => !current)}
            >
              {showAll ? t("showLess") : t("showMore", { count: hiddenCount })}
            </Button>
          ) : null}
        </Flex>
      ) : null}
    </Box>
  );
}
