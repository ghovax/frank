"use client";

import { Box, Flex, Spinner, Text } from "@chakra-ui/react";
import { useEffect, useRef } from "react";
import { LuGlobe } from "react-icons/lu";
import {
  nativeWebviewHide,
  nativeWebviewSetBounds,
  nativeWebviewShow,
  type WebviewBounds,
} from "@/lib/native-artifact";

// Round to whole logical pixels and treat sub-pixel jitter as "unchanged" so the
// rAF sync only re-invokes the native layer when the rect meaningfully moves.
function boundsChanged(a: WebviewBounds | null, b: WebviewBounds): boolean {
  if (!a) return true;
  return (
    Math.abs(a.x - b.x) > 0.5 ||
    Math.abs(a.y - b.y) > 0.5 ||
    Math.abs(a.width - b.width) > 0.5 ||
    Math.abs(a.height - b.height) > 0.5
  );
}

// The placeholder's rect, clamped to the window viewport so the native webview is
// never drawn over the app chrome outside the visible page. Returns null when the
// element is fully off-screen (e.g. scrolled away), which parks the webview.
function visibleBounds(element: HTMLElement): WebviewBounds | null {
  const rect = element.getBoundingClientRect();
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const left = Math.max(0, rect.left);
  const top = Math.max(0, rect.top);
  const right = Math.min(viewportWidth, rect.right);
  const bottom = Math.min(viewportHeight, rect.bottom);
  const width = right - left;
  const height = bottom - top;
  if (width <= 1 || height <= 1) return null;
  return { x: left, y: top, width, height };
}

// Embeds the native artifact webview over this placeholder. It measures its own rect
// every frame (cheaply — the native layer is only told when the rect actually
// changes) so the webview tracks the panel through window resizes and the panel's
// entrance animation. On unmount it parks the webview off-screen. Desktop-only; the
// caller renders this only when the native path is available.
export function NativeWebview({ url }: { url: string }) {
  const holeRef = useRef<HTMLDivElement>(null);
  const lastBoundsRef = useRef<WebviewBounds | null>(null);
  const frameRef = useRef<number | null>(null);
  const shownForUrlRef = useRef<string | null>(null);

  useEffect(() => {
    const element = holeRef.current;
    if (!element) return;
    // A fresh URL re-shows (and navigates) on the next measured rect.
    shownForUrlRef.current = null;
    lastBoundsRef.current = null;

    const sync = () => {
      frameRef.current = window.requestAnimationFrame(sync);
      const bounds = visibleBounds(element);
      if (!bounds) {
        if (lastBoundsRef.current !== null) {
          lastBoundsRef.current = null;
          shownForUrlRef.current = null;
          void nativeWebviewHide();
        }
        return;
      }
      if (shownForUrlRef.current !== url) {
        shownForUrlRef.current = url;
        lastBoundsRef.current = bounds;
        void nativeWebviewShow(url, bounds);
        return;
      }
      if (boundsChanged(lastBoundsRef.current, bounds)) {
        lastBoundsRef.current = bounds;
        void nativeWebviewSetBounds(bounds);
      }
    };

    frameRef.current = window.requestAnimationFrame(sync);
    return () => {
      if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current);
      void nativeWebviewHide();
    };
  }, [url]);

  return (
    <Box ref={holeRef} position="relative" w="100%" h="100%" minH={80} bg="bg.subtle" overflow="hidden">
      {/* Shown until the native webview paints over this hole (and again briefly on
          navigation). The native layer sits on top, so this is only ever a fallback
          backdrop, never competing with the real page. */}
      <Flex position="absolute" inset={0} align="center" justify="center" direction="column" gap={2} color="fg.subtle">
        <LuGlobe size={22} />
        <Flex align="center" gap={2}>
          <Spinner size="sm" borderWidth="2px" />
          <Text fontSize="xs">Loading page…</Text>
        </Flex>
      </Flex>
    </Box>
  );
}
