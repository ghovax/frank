"use client";

// What an agent is called: the name it was given, not the id the wire addresses it by.

import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import type { AgentSummary } from "@/lib/api";

const AgentCatalogue = createContext<Map<string, string> | null>(null);

export function AgentNamesProvider({
  agents,
  children,
}: {
  agents: AgentSummary[];
  children: ReactNode;
}) {
  const names = useMemo(
    () =>
      new Map(agents.map((agent) => [agent.id, (agent.title || agent.name || agent.id).trim()])),
    [agents],
  );
  return <AgentCatalogue.Provider value={names}>{children}</AgentCatalogue.Provider>;
}

/** The name an agent goes by, given its id. */
export function useAgentName(): (agentId: string) => string {
  const names = useContext(AgentCatalogue);
  return useCallback((agentId: string) => names?.get(agentId) || agentId, [names]);
}
