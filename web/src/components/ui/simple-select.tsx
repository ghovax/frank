"use client";

import { createListCollection, Portal, Select } from "@chakra-ui/react";
import { useMemo } from "react";

export type SelectOption = { value: string; label: string };

// The plain single-value dropdown used across the app (host, permission mode, agent, provider…): a rounded trigger with a value + chevron, and a portalled list of medium-weight items with a check on the selected one.
export function SimpleSelect({
  items,
  value,
  onValueChange,
  placeholder,
}: {
  items: SelectOption[];
  value: string;
  onValueChange: (value: string) => void;
  placeholder?: string;
}) {
  const collection = useMemo(() => createListCollection({ items }), [items]);
  return (
    // `size="xs"` is the native 32px trigger — the app's standard control height.
    <Select.Root
      collection={collection}
      value={value ? [value] : []}
      onValueChange={(details) => { const next = details.value[0]; if (next) onValueChange(next); }}
      size="xs"
    >
      <Select.Control>
        <Select.Trigger>
          <Select.ValueText placeholder={placeholder} />
        </Select.Trigger>
        <Select.IndicatorGroup>
          <Select.Indicator />
        </Select.IndicatorGroup>
      </Select.Control>
      <Portal>
        <Select.Positioner>
          <Select.Content maxH="320px" overflowY="auto">
            {items.map((item) => (
              <Select.Item item={item} key={item.value}>
                {item.label}
                <Select.ItemIndicator />
              </Select.Item>
            ))}
          </Select.Content>
        </Select.Positioner>
      </Portal>
    </Select.Root>
  );
}
