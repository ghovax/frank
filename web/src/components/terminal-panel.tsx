"use client";

import { Box } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Terminal, type ITheme } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { terminalWebSocketUrl } from "@/lib/api";
import { useColorMode } from "./ui/color-mode";
import { toaster } from "./ui/toaster";

type TerminalToastType = "error" | "warning" | "info";

function resolveCssColor(host: HTMLElement, cssValue: string, fallback: string, property: "color" | "backgroundColor" = "color"): string {
  const probe = host.ownerDocument.createElement("span");
  probe.style.position = "absolute";
  probe.style.pointerEvents = "none";
  probe.style.opacity = "0";
  probe.style[property] = cssValue;
  host.appendChild(probe);
  const resolvedColor = host.ownerDocument.defaultView?.getComputedStyle(probe)[property] || "";
  probe.remove();
  return resolvedColor && resolvedColor !== "rgba(0, 0, 0, 0)" ? resolvedColor : fallback;
}

function appTerminalTheme(host: HTMLElement): ITheme {
  const background = resolveCssColor(host, "var(--chakra-colors-bg)", "#ffffff", "backgroundColor");
  const foreground = resolveCssColor(host, "var(--chakra-colors-fg)", "#111111");
  const mutedForeground = resolveCssColor(host, "var(--chakra-colors-fg-muted)", foreground);
  const emphasizedForeground = resolveCssColor(host, "var(--chakra-colors-fg-emphasized)", foreground);
  const blue = resolveCssColor(host, "var(--chakra-colors-blue-solid)", "#2563eb");

  return {
    background,
    foreground,
    cursor: foreground,
    cursorAccent: background,
    selectionBackground: resolveCssColor(host, "var(--chakra-colors-blue-muted)", blue, "backgroundColor"),
    selectionForeground: foreground,
    black: resolveCssColor(host, "var(--chakra-colors-bg-emphasized)", background, "backgroundColor"),
    red: resolveCssColor(host, "var(--chakra-colors-red-solid)", "#dc2626"),
    green: resolveCssColor(host, "var(--chakra-colors-green-solid)", "#16a34a"),
    yellow: resolveCssColor(host, "var(--chakra-colors-yellow-solid)", "#ca8a04"),
    blue,
    magenta: resolveCssColor(host, "var(--chakra-colors-purple-solid)", "#9333ea"),
    cyan: resolveCssColor(host, "var(--chakra-colors-cyan-solid)", "#0891b2"),
    white: mutedForeground,
    brightBlack: mutedForeground,
    brightRed: resolveCssColor(host, "var(--chakra-colors-red-emphasized)", "#ef4444"),
    brightGreen: resolveCssColor(host, "var(--chakra-colors-green-emphasized)", "#22c55e"),
    brightYellow: resolveCssColor(host, "var(--chakra-colors-yellow-emphasized)", "#eab308"),
    brightBlue: resolveCssColor(host, "var(--chakra-colors-blue-emphasized)", blue),
    brightMagenta: resolveCssColor(host, "var(--chakra-colors-purple-emphasized)", "#a855f7"),
    brightCyan: resolveCssColor(host, "var(--chakra-colors-cyan-emphasized)", "#06b6d4"),
    brightWhite: emphasizedForeground,
  };
}

export function TerminalSurface({
  sessionId,
  workingDirectory,
  terminalKey = "main",
  location,
}: {
  sessionId: string | null;
  workingDirectory: string;
  terminalKey?: string;
  // When set, the terminal runs against this location (a remote one opens an SSH login
  // shell on the host, in its base dir; a local one opens there directly).
  location?: { kind: string; base_directory: string; host_alias: string };
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const errorToastKeysRef = useRef<Set<string>>(new Set());
  const [, setConnectionStatus] = useState<{ state: "connecting" | "connected" | "disconnected" | "exited"; label: string } | null>(null);
  const { colorMode } = useColorMode();
  const t = useTranslations("TerminalPanel");
  // Kept in a ref so the toast copy stays current without adding `t` (a fresh
  // function each render) to the terminal effect's deps, which would remount it.
  const translateRef = useRef(t);
  useEffect(() => {
    translateRef.current = t;
  }, [t]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const terminal = new Terminal({
      cursorBlink: true,
      fontSize: 12,
      fontWeight: "normal",
      fontWeightBold: "bold",
      letterSpacing: 0,
      scrollback: 8192,
      drawBoldTextInBrightColors: true,
      minimumContrastRatio: 4.5,
      scrollOnEraseInDisplay: true,
      theme: appTerminalTheme(host),
    });
    const fitAddon = new FitAddon();
    let socket: WebSocket | null = null;
    let fitAnimationFrame: number | null = null;
    let reconnectTimer: number | null = null;
    let socketHadError = false;
    let disposed = false;
    terminal.loadAddon(fitAddon);
    terminal.open(host);
    terminalRef.current = terminal;
    setConnectionStatus({ state: "connecting", label: "Connecting terminal" });

    const notifyTerminal = (type: TerminalToastType, title: string, description: string, key = `${type}:${title}:${description}`) => {
      if (errorToastKeysRef.current.has(key)) return;
      errorToastKeysRef.current.add(key);
      toaster.create({
        type,
        title,
        description,
        closable: true,
      });
    };

    const notifyTerminalFailure = (title: string, description: string, key = `${title}:${description}`) => {
      notifyTerminal("error", title, description, key);
    };

    const fitAndResize = () => {
      if (!host.clientWidth || !host.clientHeight) return;
      fitAddon.fit();
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "resize", rows: terminal.rows, columns: terminal.cols }));
      }
    };

    const scheduleFitAndResize = () => {
      if (fitAnimationFrame !== null) {
        cancelAnimationFrame(fitAnimationFrame);
      }
      fitAnimationFrame = requestAnimationFrame(() => {
        fitAnimationFrame = null;
        fitAndResize();
      });
    };

    const openSocket = (resetBeforeReplay = false) => {
      if (disposed || terminalRef.current !== terminal) return;
      if (resetBeforeReplay) {
        terminal.reset();
      }
      fitAndResize();
      socketHadError = false;
      setConnectionStatus({ state: "connecting", label: "Connecting terminal" });
      socket = new WebSocket(terminalWebSocketUrl({
        sessionId,
        workingDirectory,
        terminalKey,
        locationKind: location?.kind,
        locationBaseDirectory: location?.base_directory,
        locationHostAlias: location?.host_alias,
        rows: terminal.rows || 24,
        columns: terminal.cols || 80,
      }));

      socket.addEventListener("open", () => {
        fitAndResize();
        terminal.focus();
      });
      socket.addEventListener("message", (event) => {
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(String(event.data)) as Record<string, unknown>;
        } catch {
          socketHadError = true;
          setConnectionStatus(null);
          notifyTerminalFailure(translateRef.current("protocolErrorTitle"), translateRef.current("protocolErrorDescription"), "terminal_protocol_error");
          return;
        }
        if (message.type === "ready") {
          const cwd = String(message.cwd ?? (workingDirectory || "terminal"));
          setConnectionStatus({ state: "connected", label: `Connected · ${cwd}` });
          for (const key of errorToastKeysRef.current) {
            if (key.startsWith("terminal_") && !key.startsWith("terminal_closed") && !key.startsWith("terminal_exit")) {
              errorToastKeysRef.current.delete(key);
            }
          }
        } else if (message.type === "output") {
          terminal.write(String(message.data ?? ""));
        } else if (message.type === "error") {
          const code = String(message.code ?? "terminal_error");
          const detail = String(message.message ?? "Terminal connection error");
          socketHadError = true;
          setConnectionStatus(null);
          notifyTerminalFailure(translateRef.current("connectionFailedTitle"), detail, code);
        } else if (message.type === "exit") {
          const exitCode = message.exit_code;
          const exitSignal = message.exit_signal;
          const label = exitSignal != null
            ? translateRef.current("shellExitedSignal", { signal: String(exitSignal) })
            : exitCode != null
              ? translateRef.current("shellExitedCode", { code: String(exitCode) })
              : translateRef.current("shellExited");
          setConnectionStatus({ state: "exited", label });
          notifyTerminal("info", translateRef.current("processExitedTitle"), label, `terminal_exit:${String(exitCode ?? "")}:${String(exitSignal ?? "")}`);
        }
      });
      socket.addEventListener("error", () => {
        socketHadError = true;
        setConnectionStatus(null);
        notifyTerminalFailure(translateRef.current("connectionFailedTitle"), translateRef.current("websocketErrorDescription"), "terminal_websocket_error");
      });
      socket.addEventListener("close", (event) => {
        if (disposed) return;
        if (!socketHadError) {
          setConnectionStatus({ state: "disconnected", label: "Terminal disconnected · reconnecting" });
          notifyTerminal(
            "warning",
            translateRef.current("disconnectedTitle"),
            event.reason ? translateRef.current("disconnectedReason", { reason: event.reason }) : translateRef.current("disconnectedReconnecting"),
            `terminal_closed:${event.code}:${event.reason || "no_reason"}`,
          );
        }
        reconnectTimer = window.setTimeout(() => openSocket(true), 1000);
      });
    };

    const dataDisposable = terminal.onData((data) => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "input", data }));
      } else if (data) {
        notifyTerminal("warning", translateRef.current("notConnectedTitle"), translateRef.current("notConnectedDescription"), "terminal_input_disconnected");
      }
    });
    const resizeObserver = new ResizeObserver(() => scheduleFitAndResize());
    resizeObserver.observe(host);
    if (containerRef.current) {
      resizeObserver.observe(containerRef.current);
    }
    window.addEventListener("resize", scheduleFitAndResize);
    void host.ownerDocument.fonts.ready.finally(() => {
      if (terminalRef.current !== terminal) return;
      openSocket();
      scheduleFitAndResize();
    });

    return () => {
      disposed = true;
      terminalRef.current = null;
      if (fitAnimationFrame !== null) {
        cancelAnimationFrame(fitAnimationFrame);
      }
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
      resizeObserver.disconnect();
      window.removeEventListener("resize", scheduleFitAndResize);
      dataDisposable.dispose();
      if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        socket.close();
      }
      terminal.dispose();
    };
  }, [sessionId, workingDirectory, terminalKey, location?.kind, location?.base_directory, location?.host_alias]);

  useEffect(() => {
    const terminal = terminalRef.current;
    const host = hostRef.current;
    if (!terminal || !host) return;
    const animationFrame = requestAnimationFrame(() => {
      terminal.options.theme = appTerminalTheme(host);
      terminal.refresh(0, terminal.rows - 1);
    });
    return () => cancelAnimationFrame(animationFrame);
  }, [colorMode]);

  return (
    <Box
      ref={containerRef}
      className="daisy-terminal-surface"
      h="100%"
      w="100%"
      bg="bg"
      color="fg"
      overflow="hidden"
      position="relative"
    >
      {/* The xterm host is positioned with explicit insets rather than padding: xterm's
          FitAddon sizes itself to host.clientWidth/Height, so insetting the host box
          directly is what gives the terminal even breathing room on every edge. */}
      <Box
        ref={hostRef}
        position="absolute"
        top={1}
        bottom={3}
        left={2}
        right={0}
        minH={0}
        minW={0}
      />
    </Box>
  );
}
