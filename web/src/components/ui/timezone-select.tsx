"use client";

// Choosing the zone a schedule's clock runs in, from a closed set rather than by free text.

import { Combobox, Flex, Portal, Span, useFilter, useListCollection } from "@chakra-ui/react";
import { useMemo } from "react";
import { expected } from "@/lib/swallowed";

interface ZoneOption {
  /** The IANA identifier, exactly as published — the value, and what the daemon stores. */
  zone: string;
  /** The same identifier with its underscores spaced: `Africa/Addis Ababa`. What is shown. */
  label: string;
  /** The offset it is on at this moment: `GMT+2`. */
  offset: string;
  /** Whether this is the zone the machine itself is in. */
  current: boolean;
}

// Shown in the identifier's own shape, with only its underscores spaced.
function offsetOf(zone: string, at: Date): string {
  try {
    const parts = new Intl.DateTimeFormat("en", {
      timeZone: zone,
      timeZoneName: "shortOffset",
    }).formatToParts(at);
    return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
  } catch (caught) {
    expected("a zone the formatter will not accept simply shows no offset", caught);
    return "";
  }
}

/** Every zone this platform knows, with the offset each is on and the machine's own first. */
function zoneOptions(): ZoneOption[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] })
    .supportedValuesOf;
  let zones: string[];
  try {
    zones = supported ? supported("timeZone") : [];
  } catch (caught) {
    // An engine without the API still gets a working field — it just has one entry.
    expected("a platform that cannot enumerate time zones offers only the current one", caught);
    zones = [];
  }
  const machineZone = currentZone();
  const known = zones.length > 0 ? zones : [machineZone];
  const now = new Date();
  return (
    known
      .map((zone) => ({
        zone,
        label: zone.replace(/_/g, " "),
        offset: offsetOf(zone, now),
        current: zone === machineZone,
      }))
      // The machine's own zone first, since it is the answer nine times in ten, then IANA's own order.
      .sort((left, right) => {
        if (left.current !== right.current) return left.current ? -1 : 1;
        return left.zone.localeCompare(right.zone);
      })
  );
}

/** The machine's own zone — the only sensible default for "when should this fire". */
export function currentZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch (caught) {
    expected("a platform with no resolvable time zone falls back to UTC", caught);
    return "UTC";
  }
}

export function TimezoneSelect({
  value,
  onChange,
  placeholder,
  currentLabel,
}: {
  value: string;
  onChange: (zone: string) => void;
  placeholder?: string;
  /** How the machine's own zone is marked in the list. */
  currentLabel?: string;
}) {
  // Built once, because formatting 444 zones is real work and the offsets do not move while a dialog is open.
  const options = useMemo(() => zoneOptions(), []);
  // Contains rather than starts-with, since people search for the city rather than the region.
  const { contains } = useFilter({ sensitivity: "base" });
  const { collection, filter } = useListCollection<ZoneOption>({
    initialItems: options,
    itemToString: (item) => item.label,
    itemToValue: (item) => item.zone,
    filter: contains,
    // 444 rows would be mounted on every keystroke otherwise, and nobody scrolls past the first dozen.
    limit: 50,
  });

  // Seeded from the value, which is the fix for a field that opened blank.
  const selected = options.find((option) => option.zone === value);

  return (
    <Combobox.Root
      collection={collection}
      value={value ? [value] : []}
      defaultInputValue={selected ? selected.label : ""}
      onValueChange={(details) => {
        const next = details.value[0];
        if (next) onChange(next);
      }}
      onInputValueChange={(details) => filter(details.inputValue)}
      openOnClick
      selectionBehavior="replace"
      size="xs"
    >
      <Combobox.Control>
        <Combobox.Input placeholder={placeholder} />
        <Combobox.IndicatorGroup>
          <Combobox.Trigger />
        </Combobox.IndicatorGroup>
      </Combobox.Control>
      <Portal>
        <Combobox.Positioner>
          <Combobox.Content maxH="320px" overflowY="auto">
            <Combobox.Empty>{placeholder}</Combobox.Empty>
            {collection.items.map((item) => (
              <Combobox.Item item={item} key={item.zone}>
                {/* The identifier, then its marks pushed to the trailing edge, separated by layout rather than punctuation. */}
                <Combobox.ItemText>{item.label}</Combobox.ItemText>
                <Flex
                  align="center"
                  gap={3}
                  ms="auto"
                  flexShrink={0}
                  color="fg.muted"
                  fontSize="xs"
                >
                  {item.current && currentLabel ? <Span>{currentLabel}</Span> : null}
                  <Span>{item.offset}</Span>
                </Flex>
                <Combobox.ItemIndicator />
              </Combobox.Item>
            ))}
          </Combobox.Content>
        </Combobox.Positioner>
      </Portal>
    </Combobox.Root>
  );
}
