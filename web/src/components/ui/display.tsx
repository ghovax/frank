"use client";

import { Box, Flex, List, Span, Text, type SpanProps } from "@chakra-ui/react";
import { createContext, useContext, type ReactNode } from "react";
import { Pre } from "./semantic";

// Structured-display building blocks shared across the app (tool views, panels,
// dialogs): a label/value field system, monospace spans/blocks, an empty hint, and a
// bordered grouping card. One consistent visual language so every fielded surface
// lines up and new ones only declare what to show.

// The fixed width of an inline field's label column, so every label + value row lines
// up. One source of truth (InlineField) rather than a literal repeated per component.
export const FIELD_LABEL_MINIMUM_W = "70px";

export function FieldList({ children }: { children: ReactNode }) {
  return (
    // A list with nothing in it takes no room, and that is not cosmetic tidying: a field that
    // was already shown higher up renders nothing (see `FieldScope`), so a result view whose
    // every field is a repeat returns a list with no children — a real flex item, contributing
    // the parent's gap and leaving a band of empty space under the call. `:empty` is the exact
    // test, because a suppressed field leaves no node behind at all.
    <Flex direction="column" gap={2} css={{ "&:empty": { display: "none" } }}>
      {children}
    </Flex>
  );
}

// One tool row, one scope: what the call already showed, the result does not repeat.
//
// This exists because the same bug kept being fixed one view at a time. A tool call renders its
// arguments and then its result, and the two overlap by nature — `create_session` is *asked* for
// an agent and a permission mode, and *answers* with the session it made, carrying the agent and
// the permission mode back. Every such view printed both, so a person read `Agent
// code-investigator` twice, three lines apart, and there was no reason to think the next view
// would be any different: nothing prevented it.
//
// A field claims its label the first time it renders inside a row. The second attempt in the
// same row is dropped. Nothing to remember, nothing to opt into, and a new view cannot
// reintroduce the duplication because the primitive it must use is the thing that refuses.
const ShownFields = createContext<Set<string> | null>(null);

export function FieldScope({ children }: { children: ReactNode }) {
  // A fresh set per render pass, deliberately — not a ref and not memoised. The children render
  // inside this pass and claim into this pass's set, so a re-render starts from empty and every
  // field that should appear appears. A set that outlived the pass would hide fields the second
  // time the row drew itself.
  const shown = new Set<string>();
  return <ShownFields.Provider value={shown}>{children}</ShownFields.Provider>;
}

/** Whether this label is the first of its name in this row. Outside a scope, everything shows. */
function claimField(label: string, shown: Set<string> | null): boolean {
  if (!shown) return true;
  if (shown.has(label)) return false;
  shown.add(label);
  return true;
}

/** A label stacked above its value — for long values (commands, prompts, output). */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  const shown = useContext(ShownFields);
  if (!claimField(label, shown)) return null;
  return (
    <Box>
      <Text textStyle="fieldLabel" color="fg.subtle" mb={1}>
        {label}
      </Text>
      <Box fontSize="xs">{children}</Box>
    </Box>
  );
}

/** A label and value on one baseline-aligned row — for short scalar values. */

export function InlineField({ label, children, mt }: { label: string; children: ReactNode; mt?: number }) {
  const shown = useContext(ShownFields);
  if (!claimField(label, shown)) return null;
  return (
    <Flex align="baseline" gap={2} mt={mt}>
      <Text textStyle="fieldLabel" color="fg.subtle" minW={FIELD_LABEL_MINIMUM_W} flexShrink={0}>
        {label}
      </Text>
      <Box fontSize="xs" flex={1} minW={0}>
        {children}
      </Box>
    </Flex>
  );
}

// Monospace inline span for identifiers/paths/patterns/URLs — the scalar values that
// should read as code rather than prose. Extra Text props pass through for tuning.
export function Mono({ children, ...rest }: { children: ReactNode } & SpanProps) {
  return (
    <Span fontFamily="var(--app-font-mono)" fontSize="xs" wordBreak="break-all" {...rest}>
      {children}
    </Span>
  );
}

export function MonoBlock({ children, maxH = 64 }: { children: ReactNode; maxH?: number | string }) {
  return (
    <Pre
      m={0}
      fontFamily="var(--app-font-mono)"
      fontSize="xs"
      lineHeight="1.5"
      bg="bg.subtle"
      border="1px solid"
      borderColor="border"
      borderRadius="md"
      px={2}
      py={1.5}
      maxW="100%"
      maxH={maxH}
      overflowX="auto"
      overflowY="auto"
      whiteSpace="pre"
    >
      {children}
    </Pre>
  );
}

/**
 * Several monospace values as a real bullet list — one item per value.
 *
 * For a field that holds a set rather than one thing: the paths an access request names, or
 * anything else where the entries are peers. A joined string is wrong for these twice over. It
 * reads as one value when it is several, and whatever character joins them is a decision that
 * belongs to a locale, not to a component: a comma and a space is an English convention, and
 * Japanese joins with a different mark entirely. A list has no separator to get wrong.
 */
export function MonoList({ items }: { items: string[] }) {
  return (
    <List.Root pl={4} fontSize="xs" listStyleType="disc">
      {items.map((item) => (
        <List.Item key={item} mb={0.5} _last={{ mb: 0 }}>
          <Span fontFamily="var(--app-font-mono)" wordBreak="break-all">
            {item}
          </Span>
        </List.Item>
      ))}
    </List.Root>
  );
}

/**
 * Several sentences as a real bullet list — one item per sentence.
 *
 * The prose counterpart of :func:`MonoList`, and for the same reason: a goal's requirements, or
 * the evidence that each was met, are peers rather than one value with punctuation between them.
 * Monospace would be wrong here — these are sentences somebody wrote, not identifiers — which is
 * the whole of the difference between the two.
 */
export function ProseList({ items }: { items: string[] }) {
  return (
    <List.Root pl={4} fontSize="xs" listStyleType="disc">
      {items.map((item, index) => (
        <List.Item key={index} mb={0.5} _last={{ mb: 0 }}>
          {item}
        </List.Item>
      ))}
    </List.Root>
  );
}

export function EmptyHint({ children }: { children: ReactNode }) {
  return (
    <Text fontSize="xs" color="fg.subtle" fontStyle="italic">
      {children}
    </Text>
  );
}

/**
 * A bordered card used to group repeated items (steps, search results).
 *
 * It opens a field scope of its own, because it is by definition the repetition: ten search
 * results each carry a published date, and under the row's single scope only the first one
 * would have rendered — the other nine suppressed as duplicates of a label they had every
 * right to. The row's rule is "the result does not repeat what the call showed"; within a
 * card the entries are peers, not repeats, so each claims for itself.
 */
export function Card({ children }: { children: ReactNode }) {
  return (
    <Box border="1px solid" borderColor="border" borderRadius="md" bg="bg" px={2} py={1.5}>
      <FieldScope>{children}</FieldScope>
    </Box>
  );
}
