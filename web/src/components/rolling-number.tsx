"use client";

import { Box, Flex } from "@chakra-ui/react";
import { motion, useReducedMotion } from "motion/react";
import { memo, useEffect, useRef, useState } from "react";

const MotionSpan = motion.span;

function normalizedValue(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
}

// Slot-machine cadence: the counter advances one discrete notch per interval
// (each notch rolls the digits) rather than easing continuously from start to
// target. Stepping in intervals reads as "changes landing in real time" — the
// number visibly ticks up as more of the diff streams in, instead of gliding to
// the final value all at once. Capping the tick count keeps a large jump from
// dragging: a big delta simply takes bigger steps, not longer.
const SLOT_TICK_INTERVAL_MS = 75;
const SLOT_MAX_TICKS = 16;

function useAnimatedValue(target: number): { displayValue: number } {
  const prefersReducedMotion = useReducedMotion();
  const [display, setDisplay] = useState(0);
  const displayRef = useRef(0);

  useEffect(() => {
    const targetValue = normalizedValue(target);
    const startValue = displayRef.current;
    const delta = targetValue - startValue;
    if (delta === 0) return;

    const distance = Math.abs(delta);
    // Reduced motion collapses to a single notch — one `advance` lands straight on the target.
    const ticks = prefersReducedMotion ? 1 : Math.min(distance, SLOT_MAX_TICKS);
    const step = Math.ceil(distance / ticks) * Math.sign(delta);

    let interval = 0;
    const advance = () => {
      let next = displayRef.current + step;
      const overshot = step > 0 ? next >= targetValue : next <= targetValue;
      if (overshot) next = targetValue;
      displayRef.current = next;
      setDisplay(next);
      if (next === targetValue && interval) {
        window.clearInterval(interval);
      }
    };

    if (prefersReducedMotion) {
      advance();
      return;
    }

    // Fire the first notch immediately so the counter reacts the instant a change
    // lands, then keep ticking on the interval until it reaches the target.
    interval = window.setInterval(advance, SLOT_TICK_INTERVAL_MS);
    advance();
    return () => window.clearInterval(interval);
  }, [prefersReducedMotion, target]);

  return { displayValue: display };
}

const DIGITS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];

// One odometer digit: a vertical 0–9 strip inside a 1em window that translates to bring the
// target digit into view. Because it is a pure transform toward a declarative target (no
// AnimatePresence mount/exit), the shown digit always settles on the exact value — a roll can
// never leave a digit parked off-screen (the old per-digit enter animation could, which is why
// "10" sometimes rendered as "1"). A newly-appearing digit mounts already at its resting offset.
const Digit = memo(function Digit({ digit }: { digit: number }) {
  const prefersReducedMotion = useReducedMotion();
  const safeDigit = Number.isFinite(digit) ? Math.min(9, Math.max(0, Math.round(digit))) : 0;
  return (
    <Box
      as="span"
      display="inline-block"
      position="relative"
      h="1em"
      minW="0.58em"
      overflow="hidden"
      lineHeight="1em"
      fontVariantNumeric="tabular-nums"
      verticalAlign="-0.12em"
    >
      <MotionSpan
        animate={{ y: `${-safeDigit}em` }}
        transition={{ duration: prefersReducedMotion ? 0 : 0.22, ease: [0.22, 1, 0.36, 1] }}
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
        }}
      >
        {DIGITS.map((d) => (
          <span key={d} style={{ height: "1em", lineHeight: "1em", display: "flex", alignItems: "center", justifyContent: "center" }}>
            {d}
          </span>
        ))}
      </MotionSpan>
    </Box>
  );
});

interface RollingNumberProps {
  value: number;
}

export const RollingNumber = memo(function RollingNumber({
  value,
}: RollingNumberProps) {
  const { displayValue } = useAnimatedValue(value);
  const safeValue = normalizedValue(displayValue);
  const digits = String(safeValue).split("").map(Number);
  return (
    <Box
      as="span"
      display="inline-flex"
      alignItems="center"
      fontVariantNumeric="tabular-nums"
      whiteSpace="nowrap"
    >
      {digits.map((digit, index) => (
        // Keyed by place (units, tens, …) so each column stays put and rolls in place; a new
        // higher place mounts already at its resting offset.
        <Digit key={digits.length - index - 1} digit={digit} />
      ))}
    </Box>
  );
});

interface DiffStatBadgeProps {
  additions: number;
  deletions: number;
}

export function DiffStatBadge({
  additions,
  deletions,
}: DiffStatBadgeProps) {
  return (
    <Flex align="center" gap={1} flexShrink={0} fontVariantNumeric="tabular-nums">
      {additions > 0 && (
        <Box
          as="span"
          gap={1}
          color="green.fg"
          textStyle="fieldLabel"
          display="inline-flex"
          alignItems="center"
        >
          <Box as="span">+</Box>
          <RollingNumber value={additions} />
        </Box>
      )}
      {deletions > 0 && (
        <Box
          as="span"
          gap={1}
          color="red.fg"
          textStyle="fieldLabel"
          display="inline-flex"
          alignItems="center"
        >
          <Box as="span">-</Box>
          <RollingNumber value={deletions} />
        </Box>
      )}
    </Flex>
  );
}
