"use client";

// A small segmented control (two or three joined buttons in a muted track) used to switch
// a panel between views — Terminal/Processes, and similar. The look was
// duplicated per panel; it lives here once so every toggle reads and behaves identically.

import { Button, Flex } from "@chakra-ui/react";
import type { ReactNode } from "react";

export interface SegmentedOption<T extends string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
}

export function SegmentedToggle<T extends string>({
  options,
  value,
  onChange,
}: {
  options: SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <Flex align="center" bg="bg.muted" borderRadius="md" overflow="hidden" flexShrink={0}>
      {options.map((option) => {
        const active = option.value === value;
        return (
          <Button
            key={option.value}
            variant={active ? "subtle" : "ghost"}
            colorPalette={active ? "blue" : "gray"}
            borderRadius="none"
            px={2}
            gap={1}
            textStyle="fieldLabel"
            onClick={() => onChange(option.value)}
          >
            {option.icon}
            {option.label}
          </Button>
        );
      })}
    </Flex>
  );
}
