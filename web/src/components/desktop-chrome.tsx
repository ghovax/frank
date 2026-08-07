"use client";

import { Box } from "@chakra-ui/react";
import { useEffect } from "react";

// Wires up native-window chrome under Tauri, and renders nothing in a plain browser.
export function DesktopChrome() {
  useEffect(() => {
    const isTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
    if (isTauri) {
      document.documentElement.setAttribute("data-tauri", "true");
    }
    return () => {
      document.documentElement.removeAttribute("data-tauri");
    };
  }, []);

  // Always present but zero-height (and thus inert) outside the desktop app.
  return <Box className="titlebar-drag-region" data-tauri-drag-region />;
}
