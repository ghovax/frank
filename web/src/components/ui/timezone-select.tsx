"use client";

// Choosing the zone a schedule's clock runs in.
//
// It was a free-text field, which is the wrong control for a value out of a closed set of 444
// identifiers nobody spells from memory — `America/New_York` has an underscore rule, a
// capitalisation rule and a region prefix. The field's real behaviour was: type something
// plausible, save, and let the daemon reject it, after which a schedule fires unattended for
// months on whatever the retry produced.
//
// The list is the platform's own (`Intl.supportedValuesOf`), not a table shipped here that would
// go stale the next time a country changes its rules. Each row carries the zone's offset *right
// now*, because the identifier alone does not answer the question anybody actually has — "is
// that the one I mean" — and an offset reading `GMT+2` in summer and `GMT+1` in winter is the
// honest answer for a zone that observes daylight saving.

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
//
// The structure is the standard and stays: region first, `/`, then the place — what IANA
// publishes, what the daemon stores, what croniter reads, and what the schedule list shows
// afterwards. An earlier pass here rewrote `Africa/Addis_Ababa` as `Addis Ababa — Africa`, which
// invented a format nothing else uses and reordered the halves so a row no longer matched the
// value it stood for.
//
// The underscore is the one part worth touching. It is an artefact of a format that had to be
// filename-safe in 1986, it carries no meaning a reader needs, and it is the single thing that
// makes the list read as machine output. Spacing it changes nothing about the identity — the
// value sent and stored is still `Africa/Addis_Ababa` — and a search for `addis ababa` now finds
// it, which it could not before.
function offsetOf(zone: string, at: Date): string {
  try {
    const parts = new Intl.DateTimeFormat("en", { timeZone: zone, timeZoneName: "shortOffset" }).formatToParts(at);
    return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
  } catch (caught) {
    expected("a zone the formatter will not accept simply shows no offset", caught);
    return "";
  }
}

/** Every zone this platform knows, with the offset each is on and the machine's own first. */
function zoneOptions(): ZoneOption[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
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
  return known
    .map((zone) => ({
      zone,
      label: zone.replace(/_/g, " "),
      offset: offsetOf(zone, now),
      current: zone === machineZone,
    }))
    // The machine's own zone first, because it is the answer nine times in ten and scrolling to
    // it alphabetically is absurd. The rest in the order IANA lists them, which is alphabetical
    // by identifier — the same order the reader is scanning.
    .sort((left, right) => {
      if (left.current !== right.current) return left.current ? -1 : 1;
      return left.zone.localeCompare(right.zone);
    });
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
  // Built once: 444 zones each formatted for their current offset is real work, and the
  // offsets do not move while a dialog is open.
  const options = useMemo(() => zoneOptions(), []);
  // `contains` rather than `startsWith`: people search for the city, not the region, so `Rome`
  // finds Rome and `Africa` finds the continent. Base sensitivity so case and accents do not
  // matter — `sao paulo` should find São Paulo.
  const { contains } = useFilter({ sensitivity: "base" });
  const { collection, filter } = useListCollection<ZoneOption>({
    initialItems: options,
    itemToString: (item) => item.label,
    itemToValue: (item) => item.zone,
    filter: contains,
    // 444 rows would be mounted on every keystroke otherwise. Nobody scrolls past the first
    // dozen matches of a search they are typing; they narrow it instead.
    limit: 50,
  });

  // Seeded from the value, which is the fix for a field that opened blank. The draft carries
  // the machine's zone from the moment the form is created, but a combobox shows its *input*
  // text rather than its selection — so without this the schedule had a perfectly good zone
  // that the person could not see, and no way to tell it apart from having chosen nothing.
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
                {/* The identifier, then its marks pushed to the trailing edge. Separated by
                    layout rather than by punctuation: a `·` between two short strings is a
                    character doing a column's job, and it reads as noise once there are three
                    of them in a row. */}
                <Combobox.ItemText>{item.label}</Combobox.ItemText>
                <Flex align="center" gap={3} ms="auto" flexShrink={0} color="fg.muted" fontSize="xs">
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
