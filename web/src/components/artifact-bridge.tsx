"use client";

import { createContext, useContext, type ReactNode } from "react";

// An interaction posted back from a rendered artifact's sandboxed iframe. The
// frame validates the message origin before forwarding, so handlers can trust
// the shape here.
export interface ArtifactEvent {
  artifactId: string;
  title: string;
  event: string;
  data: unknown;
}

export type ArtifactEventHandler = (event: ArtifactEvent) => void;

// Artifacts render deep inside the message tree (ChatMessageItem → ToolCall →
// ToolArtifacts → RenderArtifact). A context carries the back-channel handler
// down to the iframe without threading it through every layer.
const ArtifactEventContext = createContext<ArtifactEventHandler | null>(null);

export function ArtifactEventProvider({
  onEvent,
  children,
}: {
  onEvent: ArtifactEventHandler;
  children: ReactNode;
}) {
  return <ArtifactEventContext.Provider value={onEvent}>{children}</ArtifactEventContext.Provider>;
}

export function useArtifactEvent(): ArtifactEventHandler | null {
  return useContext(ArtifactEventContext);
}
