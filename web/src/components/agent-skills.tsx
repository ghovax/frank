"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { LuListChecks, LuPlug, LuWrench } from "react-icons/lu";
import { fetchMcpTools, type AgentCard, type AgentSkill, type McpServerTools, type McpTool } from "@/lib/api";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolMetaRow } from "./tool-card";

// Renders a capability's display title: the human name when present, otherwise a
// fallback to its identifier rendered in monospace to signal it is an id, not a
// name. A name equal to the identifier is treated as a fallback (the backend
// defaults a missing name to the id), so it too renders in monospace.
function CapabilityTitle({ name, identifier }: { name?: string | null; identifier: string }) {
  const display = (name ?? "").trim();
  if (display && display !== identifier) return <>{display}</>;
  return <Box as="span" fontFamily="var(--app-font-mono)">{identifier}</Box>;
}

// Shows the selected agent's A2A AgentCard skills — broadcast from the served
// agent and rendered as collapsible cards, so you can see what an agent can do —
// plus the tools exposed by the configured MCP servers, grouped per server.
// Every card starts collapsed to keep the empty state uncluttered.
export function AgentSkills({ card }: { card: AgentCard | null }) {
  const [mcpServers, setMcpServers] = useState<McpServerTools[]>([]);

  useEffect(() => {
    let cancelled = false;
    fetchMcpTools()
      .then((servers) => {
        if (!cancelled) setMcpServers(servers);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const hasSkills = !!card && card.skills.length > 0;
  const toolServers = mcpServers.filter((server) => server.tools.length > 0);
  const hasTools = toolServers.length > 0;
  if (!hasSkills && !hasTools) return null;

  return (
    <Box w="100%" maxW="640px" mx="auto">
      {hasSkills && (
        <>
          <Flex align="center" gap={1.5} mb={2} color="fg.muted">
            <LuListChecks size={13} />
            <Text fontSize="xs" fontWeight="bold">Available capabilities</Text>
          </Flex>
          {card!.description && (
            <Text fontSize="xs" color="fg.muted" mb={2}>{card!.description}</Text>
          )}
          <Flex direction="column" gap={2}>
            {card!.skills.map((skill) => (
              <SkillCard key={skill.id} skill={skill} />
            ))}
          </Flex>
        </>
      )}

      {hasTools && (
        <Box mt={hasSkills ? 5 : 0}>
          <Flex align="center" gap={1.5} mb={2} color="fg.muted">
            <LuWrench size={13} />
            <Text fontSize="xs" fontWeight="bold">Tools</Text>
          </Flex>
          <Flex direction="column" gap={2}>
            {toolServers.map((server) => (
              <McpServerGroup key={server.name} server={server} />
            ))}
          </Flex>
        </Box>
      )}
    </Box>
  );
}

// One agent skill, collapsed by default like a tool-call card.
function SkillCard({ skill }: { skill: AgentSkill }) {
  const [open, setOpen] = useState(false);
  const hasBody = !!skill.description || (skill.examples?.length ?? 0) > 0;
  return (
    <ToolCard>
      <ToolCardHeader
        collapsible={hasBody}
        open={open}
        onToggle={() => setOpen((value) => !value)}
        icon={<Box color="fg.muted"><LuWrench size={12} /></Box>}
        title={<CapabilityTitle name={skill.name} identifier={skill.id} />}
      />
      {open && hasBody && (
        <ToolCardBody>
          {skill.description && (
            <Text fontSize="xs" color="fg.muted">{skill.description}</Text>
          )}
          {skill.examples && skill.examples.length > 0 && (
            <Box mt={2}>
              <ToolMetaRow label="Examples">
                <Flex direction="column" gap={0.5}>
                  {skill.examples.map((example, index) => (
                    <Text key={index} fontSize="xs" color="fg.muted">“{example}”</Text>
                  ))}
                </Flex>
              </ToolMetaRow>
            </Box>
          )}
        </ToolCardBody>
      )}
    </ToolCard>
  );
}

// One MCP server's tools, collapsed by default like a tool-call card.
function McpServerGroup({ server }: { server: McpServerTools }) {
  const [open, setOpen] = useState(false);
  return (
    <ToolCard>
      <ToolCardHeader
        collapsible
        open={open}
        onToggle={() => setOpen((value) => !value)}
        icon={<Box color="fg.muted"><LuPlug size={12} /></Box>}
        title={<CapabilityTitle identifier={server.name} />}
        badges={
          <Text fontSize="xs" color="fg.subtle" flexShrink={0}>
            {server.tools.length} {server.tools.length === 1 ? "tool" : "tools"}
          </Text>
        }
      />
      {open && (
        <ToolCardBody>
          <Flex direction="column" gap={2}>
            {server.tools.map((tool) => (
              <McpToolRow key={tool.name} tool={tool} />
            ))}
          </Flex>
        </ToolCardBody>
      )}
    </ToolCard>
  );
}

// A single MCP tool: its human title when present, else its name (id) in monospace.
function McpToolRow({ tool }: { tool: McpTool }) {
  return (
    <Box>
      <Text fontSize="xs" fontWeight="medium">
        <CapabilityTitle name={tool.title} identifier={tool.name} />
      </Text>
      {tool.description && (
        <Text fontSize="xs" color="fg.muted">{tool.description}</Text>
      )}
    </Box>
  );
}
