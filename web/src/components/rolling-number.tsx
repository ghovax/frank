"use client";

import { Box, Flex } from "@chakra-ui/react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { memo, useEffect, useRef, useState } from "react";

const MotionSpan = motion.span;

function normalizedValue(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
}

function useAnimatedValue(target: number, duration = 520): { displayValue: number; direction: 1 | -1 } {
  const prefersReducedMotion = useReducedMotion();
  const [display, setDisplay] = useState(0);
  const [direction, setDirection] = useState<1 | -1>(1);
  const displayRef = useRef(0);

  useEffect(() => {
    const targetValue = normalizedValue(target);
    if (prefersReducedMotion) {
      displayRef.current = targetValue;
      setDisplay(targetValue);
      return;
    }

    const startValue = displayRef.current;
    const delta = targetValue - startValue;
    if (delta === 0) {
      setDisplay(targetValue);
      return;
    }

    setDirection(delta > 0 ? 1 : -1);
    const startTime = performance.now();
    const distanceAdjustedDuration = Math.min(760, Math.max(220, duration + Math.min(Math.abs(delta), 24) * 8));
    let animationFrame: number;

    function tick(now: number) {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / distanceAdjustedDuration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      const currentValue = Math.round(startValue + delta * eased);

      displayRef.current = currentValue;
      setDisplay(currentValue);

      if (progress < 1) {
        animationFrame = requestAnimationFrame(tick);
      } else {
        displayRef.current = targetValue;
        setDisplay(targetValue);
      }
    }

    animationFrame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animationFrame);
  }, [duration, prefersReducedMotion, target]);

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
          gap={0.25}
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
