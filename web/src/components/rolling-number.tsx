"use client";

import { Span } from "@chakra-ui/react";
import { motion, useReducedMotion } from "motion/react";
import { memo, useEffect, useRef, useState } from "react";

const MotionSpan = motion.span;

function normalizedValue(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
}

// Slot-machine cadence: the counter advances one discrete notch per interval rather than easing.
const SLOT_TICK_INTERVAL_MS = 75;
const SLOT_MAXIMUM_TICKS = 16;

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
    const ticks = prefersReducedMotion ? 1 : Math.min(distance, SLOT_MAXIMUM_TICKS);
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

    // Fire the first notch immediately so the counter reacts at once, then tick to the target.
    interval = window.setInterval(advance, SLOT_TICK_INTERVAL_MS);
    advance();
    return () => window.clearInterval(interval);
  }, [prefersReducedMotion, target]);

  return { displayValue: display };
}

const DIGITS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];

// One odometer digit: a vertical strip inside a one-em window that translates the target into view.
const Digit = memo(function Digit({ digit }: { digit: number }) {
  const prefersReducedMotion = useReducedMotion();
  const safeDigit = Number.isFinite(digit) ? Math.min(9, Math.max(0, Math.round(digit))) : 0;
  return (
    <Span
      display="inline-block"
      position="relative"
      h="1em"
      minW="0.58em"
      overflow="hidden"
      lineHeight="1em"
      fontVariantNumeric="tabular-nums"
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
        {DIGITS.map((digit) => (
          <Span
            key={digit}
            h="1em"
            lineHeight="1em"
            display="flex"
            alignItems="center"
            justifyContent="center"
          >
            {digit}
          </Span>
        ))}
      </MotionSpan>
    </Span>
  );
});

interface RollingNumberProps {
  value: number;
}

export const RollingNumber = memo(function RollingNumber({ value }: RollingNumberProps) {
  const { displayValue } = useAnimatedValue(value);
  const safeValue = normalizedValue(displayValue);
  const digits = String(safeValue).split("").map(Number);
  return (
    <Span
      display="inline-flex"
      alignItems="center"
      fontVariantNumeric="tabular-nums"
      whiteSpace="nowrap"
    >
      {digits.map((digit, index) => (
        // Keyed by place, so each column stays put and a new higher place mounts already at rest.
        <Digit key={digits.length - index - 1} digit={digit} />
      ))}
    </Span>
  );
});
