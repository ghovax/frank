"use client";

import { Box, Flex } from "@chakra-ui/react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
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

function useAnimatedValue(target: number): { displayValue: number; direction: 1 | -1 } {
  const prefersReducedMotion = useReducedMotion();
  const [display, setDisplay] = useState(0);
  const [direction, setDirection] = useState<1 | -1>(1);
  const displayRef = useRef(0);

  useEffect(() => {
    const targetValue = normalizedValue(target);
    const startValue = displayRef.current;
    const delta = targetValue - startValue;
    if (delta === 0) return;

    setDirection(delta > 0 ? 1 : -1);

    if (prefersReducedMotion) {
      displayRef.current = targetValue;
      setDisplay(targetValue);
      return;
    }

    const distance = Math.abs(delta);
    const ticks = Math.min(distance, SLOT_MAX_TICKS);
    const step = Math.ceil(distance / ticks) * Math.sign(delta);

    const advance = () => {
      let next = displayRef.current + step;
      const overshot = step > 0 ? next >= targetValue : next <= targetValue;
      if (overshot) next = targetValue;
      displayRef.current = next;
      setDisplay(next);
      if (next === targetValue) {
        window.clearInterval(interval);
      }
    };

    // Fire the first notch immediately so the counter reacts the instant a change
    // lands, then keep ticking on the interval until it reaches the target.
    const interval = window.setInterval(advance, SLOT_TICK_INTERVAL_MS);
    advance();
    return () => window.clearInterval(interval);
  }, [prefersReducedMotion, target]);

  return { displayValue: display, direction };
}

const Digit = memo(function Digit({ digit, direction }: { digit: number; direction: 1 | -1 }) {
  const safeDigit = Number.isFinite(digit) ? digit : 0;
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
      <AnimatePresence initial={false} custom={direction} mode="popLayout">
        <MotionSpan
          key={safeDigit}
          custom={direction}
          initial={{ y: direction > 0 ? "100%" : "-100%", opacity: 0.3 }}
          animate={{ y: "0%", opacity: 1 }}
          exit={{ y: direction > 0 ? "-100%" : "100%", opacity: 0.3 }}
          transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
          style={{
            position: "absolute",
            inset: 0,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {safeDigit}
        </MotionSpan>
      </AnimatePresence>
    </Box>
  );
});

interface RollingNumberProps {
  value: number;
}

export const RollingNumber = memo(function RollingNumber({
  value,
}: RollingNumberProps) {
  const { displayValue, direction } = useAnimatedValue(value);
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
        <Digit key={digits.length - index - 1} digit={digit} direction={direction} />
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
          gap={0.5}
          color="green.fg"
          fontWeight="semibold"
          fontSize="xs"
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
          gap={0.5}
          color="red.fg"
          fontWeight="semibold"
          fontSize="xs"
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
