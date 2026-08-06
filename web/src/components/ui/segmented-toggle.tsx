"use client";

// A small segmented control for switching a panel between views, defined once so they cannot drift.

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
