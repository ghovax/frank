"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { LuListChecks, LuPlug, LuPuzzle, LuWrench } from "react-icons/lu";
import { fetchMcpTools, subscribeEvents, type AgentCard, type AgentSkill, type McpServerTools, type McpTool } from "@/lib/api";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolMetaRow } from "./tool-card";
import { MarkdownContent } from "./markdown-content";

// Renders a capability's display title: the human title when present, otherwise a
// fallback to its identifier rendered in monospace to signal it is an id, not a
// display title.
function CapabilityTitle({ title, identifier }: { title?: string | null; identifier: string }) {
  const display = (title ?? "").trim();
  if (display && display !== identifier) return <>{display}</>;
  return <Box as="span" fontFamily="var(--app-font-mono)">{identifier}</Box>;
}

// MCP tool descriptions come from Python docstrings, which carry an Args:/
// Returns:/etc. section that duplicates the tool's input schema. For the
// capability browser we only show the human-readable summary above it.
const DOCSTRING_SECTION = /\n[ \t]*(Args|Arguments|Parameters|Params|Returns|Yields|Raises|Examples?|Notes?|See Also|References|Todo|Warnings?)(\s*\([^)]*\))?\s*:/i;

function docstringSummary(description: string): string {
  const match = description.match(DOCSTRING_SECTION);
  return match ? description.slice(0, match.index ?? 0).trim() : description.trim();
}

// Comparator that pushes disabled capabilities (skills or servers) to the end
// while preserving the relative order of everything else.
function disabledLast(first: { enabled?: boolean }, second: { enabled?: boolean }): number {
  return Number(first.enabled === false) - Number(second.enabled === false);
}

// Shows the selected agent's A2A AgentCard skills — broadcast from the served
// agent and rendered as collapsible cards, so you can see what an agent can do —
// plus the tools exposed by the configured MCP servers, grouped per server.
// Every card starts collapsed to keep the empty state uncluttered.
export function AgentSkills({ card, workingDirectory }: { card: AgentCard | null; workingDirectory?: string }) {
  const [mcpServers, setMcpServers] = useState<McpServerTools[]>([]);

  useEffect(() => {
    let cancelled = false;
    // MCP servers are scoped to the selected folder (its own mcp.json plus the
    // home globals and Composio), so refetch whenever that folder changes.
    const loadTools = () => {
      fetchMcpTools(workingDirectory)
        .then((servers) => {
          if (!cancelled) setMcpServers(servers);
        })
        .catch(() => {});
    };
    loadTools();
    // MCP servers reload live (mcp.json is watched server-side); refetch the tool
    // list when the server signals a change so it stays current without a reload.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") loadTools();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [workingDirectory]);

  const hasSkills = !!card && card.skills.length > 0;
  // Disabled capabilities are shown greyed out but sorted to the bottom of their
  // list so they do not clutter the active ones (stable: relative order is kept).
  const skills = card ? [...card.skills].sort(disabledLast) : [];
  // Disabled servers are shown (greyed out) rather than hidden; enabled servers
  // still connecting (no tools yet) stay hidden until they advertise something.
  const toolServers = mcpServers
    .filter((server) => server.enabled === false || server.tools.length > 0)
    .sort(disabledLast);
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
            <Box mb={2} color="fg.muted">
              <MarkdownContent content={card!.description} fontSize="xs" />
            </Box>
          )}
          <Flex direction="column" gap={2}>
            {skills.map((skill) => (
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
          <Box mb={2} color="fg.muted">
            <Text fontSize="xs">External tools the agent can call, exposed by the configured MCP servers and grouped by server.</Text>
          </Box>
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

// A small chip marking a capability the agent cannot currently use.
function DisabledBadge() {
  return (
    <Badge size="sm" variant="subtle" colorPalette="gray" borderRadius="sm" flexShrink={0}>
      Disabled
    </Badge>
  );
}

// One agent skill, collapsed by default like a tool-call card. A disabled skill
// is rendered greyed out and non-interactive (it cannot be expanded).
function SkillCard({ skill }: { skill: AgentSkill }) {
  const [open, setOpen] = useState(false);
  const enabled = skill.enabled !== false;
  const hasBody = !!skill.description || (skill.examples?.length ?? 0) > 0;
  const collapsible = enabled && hasBody;
  return (
    <Box opacity={enabled ? 1 : 0.55}>
    <ToolCard>
      <ToolCardHeader
        collapsible={collapsible}
        open={open}
        onToggle={() => setOpen((value) => !value)}
        icon={<Box color="fg.muted"><LuPuzzle size={12} /></Box>}
        title={<CapabilityTitle title={skill.title ?? skill.name} identifier={skill.id} />}
        badges={enabled ? undefined : <DisabledBadge />}
      />
      {open && collapsible && (
        <ToolCardBody>
          {skill.description && (
            <Box color="fg.muted">
              <MarkdownContent content={skill.description} fontSize="xs" />
            </Box>
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
    </Box>
  );
}

// One MCP server's tools, collapsed by default like a tool-call card. A disabled
// server is rendered greyed out and non-interactive (it cannot be expanded).
function McpServerGroup({ server }: { server: McpServerTools }) {
  const [open, setOpen] = useState(false);
  const enabled = server.enabled !== false;
  return (
    <Box opacity={enabled ? 1 : 0.55}>
    <ToolCard>
      <ToolCardHeader
        collapsible={enabled}
        open={open}
        onToggle={() => setOpen((value) => !value)}
        icon={<Box color="fg.muted"><LuPlug size={12} /></Box>}
        title={<CapabilityTitle identifier={server.name} />}
        badges={
          !enabled ? (
            <DisabledBadge />
          ) : (
          <Text fontSize="xs" color="fg.subtle" flexShrink={0} fontWeight="medium">
            Provides a total of {server.tools.length} {server.tools.length === 1 ? "tool" : "tools"}
          </Text>
          )
        }
      />
      {enabled && open && (
        <ToolCardBody>
          <Flex direction="column" gap={2}>
            {server.tools.map((tool) => (
              <McpToolRow key={tool.name} tool={tool} />
            ))}
          </Flex>
        </ToolCardBody>
      )}
    </ToolCard>
    </Box>
  );
}

// A single MCP tool: its human title when present, else its name (id) in monospace.
function McpToolRow({ tool }: { tool: McpTool }) {
  return (
    <Box>
      <Text fontSize="xs" fontWeight="medium">
        <CapabilityTitle title={tool.title} identifier={tool.name} />
      </Text>
      {tool.description && (
        <Box color="fg.muted">
          <MarkdownContent content={docstringSummary(tool.description)} fontSize="xs" />
        </Box>
      )}
    </Box>
  );
}
