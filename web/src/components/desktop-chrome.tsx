"use client";

import { Box } from "@chakra-ui/react";
import { useEffect } from "react";

// Wires up the native-window chrome when the UI runs inside the Tauri desktop
// app. In a plain browser it renders nothing and marks nothing, so the web build
// is unaffected. When Tauri is detected it sets `html[data-tauri="true"]`, which
// switches on the `--titlebar-height` inset (see globals.css), and renders the
// draggable strip that lets the frameless window be moved by its top edge.
export function DesktopChrome() {
  useEffect(() => {
    const isTauri =
      typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
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
