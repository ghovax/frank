"use client";

import { createContext, useContext, type ReactNode } from "react";

// An interaction posted back from a rendered widget's sandboxed iframe. The
// frame validates the message origin before forwarding, so handlers can trust
// the shape here.
export interface WidgetEvent {
  artifactId: string;
  title: string;
  event: string;
  data: unknown;
}

export type WidgetEventHandler = (event: WidgetEvent) => void;

// Widgets render deep inside the message tree (ChatMessageItem → ToolCall →
// ToolArtifacts → RenderArtifact). A context carries the back-channel handler
// down to the iframe without threading it through every layer.
const WidgetEventContext = createContext<WidgetEventHandler | null>(null);

export function WidgetEventProvider({
  onEvent,
  children,
}: {
  onEvent: WidgetEventHandler;
  children: ReactNode;
}) {
  return <WidgetEventContext.Provider value={onEvent}>{children}</WidgetEventContext.Provider>;
}

export function useWidgetEvent(): WidgetEventHandler | null {
  return useContext(WidgetEventContext);
}
