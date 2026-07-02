"use client";

import { Box, Flex } from "@chakra-ui/react";
import { memo, useEffect, useRef, useState } from "react";

// Animates from 0 (on mount) or the previous value to the new target using
// requestAnimationFrame, so the number visibly counts up as edits arrive.
function useAnimatedValue(target: number, duration = 450): number {
  const [display, setDisplay] = useState(0);
  const previousRef = useRef(0);

  useEffect(() => {
    if (!Number.isFinite(target)) {
      previousRef.current = 0;
      setDisplay(0);
      return;
    }

    if (target === previousRef.current) {
      setDisplay(target);
      return;
    }

    const startValue = previousRef.current;
    const startTime = performance.now();
    let frameId: number;

    function tick(now: number) {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      // Ease-out cubic: fast start, slow end
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = Math.round(startValue + (target - startValue) * eased);

      setDisplay(current);

      if (progress < 1) {
        frameId = requestAnimationFrame(tick);
      } else {
        previousRef.current = target;
      }
    }

    frameId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, duration]);

  return display;
}

// A single digit — rendered as plain text since the counter hook drives the
// visible stepping animation (the old cylinder-based approach didn't animate
// because values jumped directly to the final number without intermediate steps).
const Digit = memo(function Digit({ digit }: { digit: number }) {
  const safeDigit = Number.isFinite(digit) ? digit : 0;
  return (
    <Box
      as="span"
      display="inline-flex"
      h="1em"
      minW="0.5em"
      alignItems="center"
      justifyContent="center"
      fontVariantNumeric="tabular-nums"
    >
      {safeDigit}
    </Box>
  );
});

interface RollingNumberProps {
  value: number;
}

export const RollingNumber = memo(function RollingNumber({
  value,
}: RollingNumberProps) {
  const displayValue = useAnimatedValue(value);
  const safeValue = Number.isFinite(displayValue) ? Math.max(0, displayValue) : 0;
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
        <Digit key={index} digit={digit} />
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
