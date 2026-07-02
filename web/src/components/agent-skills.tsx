"use client";

import { Badge, Box, EmptyState, Flex, Text } from "@chakra-ui/react";
import { useEffect, useState, type ReactNode } from "react";
import { LuListChecks, LuPlug, LuPuzzle, LuWrench } from "react-icons/lu";
import { fetchMcpTools, fetchSkills, subscribeEvents, type AgentCard, type AgentSkill, type McpServerTools, type McpTool } from "@/lib/api";
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
export function AgentSkills({ card, workingDirectory, homeDirectory }: { card: AgentCard | null; workingDirectory?: string; homeDirectory?: string }) {
  const [mcpServers, setMcpServers] = useState<McpServerTools[]>([]);
  const [folderSkills, setFolderSkills] = useState<AgentSkill[]>([]);

  useEffect(() => {
    let cancelled = false;
    // Skills and MCP servers are both scoped to the selected folder (home globals
    // plus that folder's own `.agents`), so refetch whenever the folder changes.
    // Skills are listed independently of any agent, so a folder's global skills
    // still appear even when it has no agents.
    const loadCapabilities = () => {
      fetchSkills(workingDirectory)
        .then((skills) => {
          if (!cancelled) setFolderSkills(skills);
        })
        .catch(() => {});
      fetchMcpTools(workingDirectory)
        .then((servers) => {
          if (!cancelled) setMcpServers(servers);
        })
        .catch(() => {});
    };
    loadCapabilities();
    // Skills and MCP servers reload live (their files are watched server-side);
    // refetch when the server signals a change so they stay current.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") loadCapabilities();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [workingDirectory]);

  // Disabled capabilities are shown greyed out but sorted to the bottom of their
  // list so they do not clutter the active ones (stable: relative order is kept).
  const skills = [...folderSkills].sort(disabledLast);
  const hasSkills = skills.length > 0;
  // Disabled servers are shown (greyed out) rather than hidden; enabled servers
  // still connecting (no tools yet) stay hidden until they advertise something.
  const toolServers = mcpServers
    .filter((server) => server.enabled === false || server.tools.length > 0)
    .sort(disabledLast);
  const hasTools = toolServers.length > 0;
  if (!hasSkills && !hasTools) return null;

  // Split each list into the global capabilities (from ~/.agents) and the ones the
  // selected folder contributes itself, so the two scopes can be shown apart. The
  // scope labels only appear once the folder actually adds something project-local;
  // a plain folder (only globals, e.g. home) stays an unlabelled flat list.
  const globalSkills = skills.filter((skill) => skill.scope !== "project");
  const projectSkills = skills.filter((skill) => skill.scope === "project");
  const globalServers = toolServers.filter((server) => server.scope !== "project");
  const projectServers = toolServers.filter((server) => server.scope === "project");

  // The home folder has no project scope of its own, so its "This project" group
  // (which would always be empty) is suppressed — only real project folders show it.
  const isHomeFolder = !workingDirectory || workingDirectory === homeDirectory;

  return (
    <Box w="100%" maxW="640px" mx="auto" pb={4}>
      {hasSkills && (
        <>
          <Flex align="center" gap={1.5} mb={2} color="fg.muted">
            <LuListChecks size={15} />
            <Text fontSize="sm" fontWeight="bold">Skills</Text>
          </Flex>
          <Box mb={2} color="fg.muted">
            <Text fontSize="xs">Capabilities available to assist with your tasks.</Text>
          </Box>
          <Flex direction="column" gap={2}>
            <ScopeLabel icon={<LuPuzzle size={12} />}>Skills available globally</ScopeLabel>
            {globalSkills.length > 0
              ? globalSkills.map((skill) => <SkillCard key={skill.id} skill={skill} />)
              : <EmptyScope icon={<LuPuzzle />}>No global skills</EmptyScope>}
            {!isHomeFolder && <ScopeLabel icon={<LuPuzzle size={12} />}>Skills available in this project</ScopeLabel>}
            {!isHomeFolder && (projectSkills.length > 0
              ? projectSkills.map((skill) => <SkillCard key={skill.id} skill={skill} />)
              : <EmptyScope icon={<LuPuzzle />}>No project-specific skills</EmptyScope>)}
          </Flex>
        </>
      )}

      {hasTools && (
        <Box mt={hasSkills ? 5 : 0}>
          <Flex align="center" gap={1.5} mb={2} color="fg.muted">
            <LuWrench size={15} />
            <Text fontSize="sm" fontWeight="bold">Tools</Text>
          </Flex>
          <Box mb={2} color="fg.muted">
            <Text fontSize="xs">External tools the agent can call, exposed by the configured MCP servers and grouped by server.</Text>
          </Box>
          <Flex direction="column" gap={2}>
            <ScopeLabel icon={<LuPlug size={12} />}>Tools available globally</ScopeLabel>
            {globalServers.length > 0
              ? globalServers.map((server) => <McpServerGroup key={server.name} server={server} />)
              : <EmptyScope icon={<LuPlug />}>No global tools</EmptyScope>}
            {!isHomeFolder && <ScopeLabel icon={<LuPlug size={12} />}>Tools available in this project</ScopeLabel>}
            {!isHomeFolder && (projectServers.length > 0
              ? projectServers.map((server) => <McpServerGroup key={server.name} server={server} />)
              : <EmptyScope icon={<LuPlug />}>No project-specific tools</EmptyScope>)}
          </Flex>
        </Box>
      )}
    </Box>
  );
}

// A plain label separating the global capabilities from the ones the selected
// project contributes itself. Always shown (even in the home folder) so the two
// scopes read clearly; deliberately understated, not a bold uppercase heading.
function ScopeLabel({ icon, children }: { icon?: ReactNode; children: string }) {
  return (
    <Flex align="center" gap={1.5} mt={1}>
      {icon && <Box color="fg.subtle" flexShrink={0}>{icon}</Box>}
      <Text fontSize="xs" fontWeight="medium" color="fg.subtle">
        {children}
      </Text>
    </Flex>
  );
}

// Placeholder for a scope that currently has no capabilities (e.g. a project with
// no project-specific skills yet), matching the empty state used elsewhere so its
// label is not left dangling.
function EmptyScope({ icon, children }: { icon: ReactNode; children: string }) {
  return (
    <EmptyState.Root size="sm">
      <EmptyState.Content>
        <EmptyState.Indicator>{icon}</EmptyState.Indicator>
        <EmptyState.Title fontSize="xs">{children}</EmptyState.Title>
      </EmptyState.Content>
    </EmptyState.Root>
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
            {server.tools.length} {server.tools.length === 1 ? "tool" : "tools"}
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
