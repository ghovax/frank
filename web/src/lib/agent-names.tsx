"use client";

// What an agent is called, wherever the interface has to say it.
//
// A profile has two names: the id it is addressed by (`code-investigator`) and the name it was
// given (`Code investigator`). The first is the wire's, and it kept surfacing in places a person
// reads — a tool call announcing which agent a peer runs showed the slug, while the sidebar's
// hover card three inches away showed the name, from the same catalogue.
//
// A context rather than a prop threaded through the transcript, because the components that need
// it are leaves — a field inside a tool result inside a group — and every one of them would
// otherwise have to be handed a list it has no other use for. The catalogue is already loaded
// once, at the top; this makes it reachable without passing it through six components that do
// not care.
//
// Outside a provider it answers with the id, which is the honest fallback: better a slug than a
// blank where a name should be.

import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import type { AgentSummary } from "@/lib/api";

const AgentCatalogue = createContext<Map<string, string> | null>(null);

export function AgentNamesProvider({ agents, children }: { agents: AgentSummary[]; children: ReactNode }) {
  const names = useMemo(
    () => new Map(agents.map((agent) => [agent.id, (agent.title || agent.name || agent.id).trim()])),
    [agents],
  );
  return <AgentCatalogue.Provider value={names}>{children}</AgentCatalogue.Provider>;
}

/** The name an agent goes by, given its id. */
export function useAgentName(): (agentId: string) => string {
  const names = useContext(AgentCatalogue);
  return useCallback((agentId: string) => names?.get(agentId) || agentId, [names]);
}
