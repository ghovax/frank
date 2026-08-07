"use client";

// Inline renderer for mermaid fences, replacing the code block once the source parses.

import { Box } from "@chakra-ui/react";
import { useEffect, useState, type ReactNode } from "react";
import { useColorMode } from "./ui/color-mode";

// One shared module promise, since mermaid is heavy and is fetched on the first diagram only.
let mermaidModule: Promise<typeof import("mermaid")> | null = null;
function loadMermaid() {
  mermaidModule ??= import("mermaid");
  return mermaidModule;
}

// mermaid.render needs a document-unique element id per call.
let renderSequence = 0;

// How long the source must hold still before a render is attempted, since streaming text is rarely valid.
const RENDER_DEBOUNCE_MS = 150;

export function MermaidDiagram({ code, fallback }: { code: string; fallback: ReactNode }) {
  const { colorMode } = useColorMode();
  const [svg, setSvg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(async () => {
      const { default: mermaid } = await loadMermaid();
      if (cancelled) return;
      // The grayscale palette sits with the app's language better, and diagram text is treated as untrusted.
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
        // Parse first, since it validates without the render's DOM side effects.
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
      // The figure fades in once when it first replaces the code block, and never again.
      className="reveal-enter"
      my={1.5}
      px={3}
      py={2.5}
      borderWidth="1px"
      borderColor="border.muted"
      borderRadius="md"
      bg="bg"
      maxW="100%"
      overflowX="auto"
      // The diagram scales down to the column but never past its natural size, and centres.
      css={{
        "& svg": { maxWidth: "100%", height: "auto", display: "block", marginInline: "auto" },
      }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
