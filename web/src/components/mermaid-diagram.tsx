"use client";

// Inline renderer for ```mermaid fenced blocks. The diagram replaces the code
// block in the prose once the source parses; until then (a diagram still
// streaming in, or one that never parses) the caller's `fallback` — the
// standard code block — stays up, so broken source remains inspectable instead
// of becoming an error graphic. A successful render is kept when a later edit
// stops parsing (mid-stream states pass through invalid text constantly), so
// the diagram updates smoothly rather than flashing back to code.

import { Box } from "@chakra-ui/react";
import { useEffect, useState, type ReactNode } from "react";
import { useColorMode } from "./ui/color-mode";

// One shared module promise: mermaid is heavy, so it is code-split and fetched
// on the first diagram only, then reused by every later one.
let mermaidModule: Promise<typeof import("mermaid")> | null = null;
function loadMermaid() {
  mermaidModule ??= import("mermaid");
  return mermaidModule;
}

// mermaid.render needs a document-unique element id per call.
let renderSequence = 0;

// How long the source must hold still before a render is attempted. A streaming
// message re-renders ~15×/s and mid-stream mermaid text is almost always invalid;
// debouncing keeps the parser off the hot path until the block settles.
const RENDER_DEBOUNCE_MS = 150;

export function MermaidDiagram({ code, fallback }: { code: string; fallback: ReactNode }) {
  const { colorMode } = useColorMode();
  const [svg, setSvg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(async () => {
      const { default: mermaid } = await loadMermaid();
      if (cancelled) return;
      // `neutral` in light mode — the grayscale palette sits with the app's
      // hairline-and-muted language better than the default blue. Strict
      // security: diagram text is model/chat content. Edge labels take the
      // app's real background (resolved from the token, since mermaid can't
      // read CSS variables) so they sit on their edge without a gray box.
      const pageBackground = getComputedStyle(document.documentElement)
        .getPropertyValue("--chakra-colors-bg")
        .trim();
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: colorMode === "dark" ? "dark" : "neutral",
        fontFamily: "var(--app-font-sans)",
        themeVariables: pageBackground ? { edgeLabelBackground: pageBackground } : undefined,
      });
      try {
        // parse() first: it validates without mermaid.render's DOM side effects
        // (an orphaned error element per failed attempt).
        await mermaid.parse(code);
        const { svg: rendered } = await mermaid.render(`mermaid-${++renderSequence}`, code);
        if (!cancelled) setSvg(rendered);
      } catch {
        // Invalid (often just incomplete) source: keep whatever rendered last.
      }
    }, RENDER_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [code, colorMode]);

  if (!svg) return <>{fallback}</>;
  return (
    <Box
      my={1.5}
      px={3}
      py={2.5}
      borderWidth="1px"
      borderColor="border.muted"
      borderRadius="md"
      bg="bg"
      maxW="100%"
      overflowX="auto"
      // The SVG scales down to the column but never up past its natural size,
      // and centers — a diagram is a figure, not a text block.
      css={{ "& svg": { maxWidth: "100%", height: "auto", display: "block", marginInline: "auto" } }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
