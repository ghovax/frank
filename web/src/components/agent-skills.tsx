"use client";

import { Box, Flex, Span, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { LuListChecks, LuPlug, LuPuzzle, LuWrench } from "react-icons/lu";
import { fetchMcpTools, fetchSkills, subscribeEvents, type AgentCard, type AgentSkill, type McpServerTools, type McpTool } from "@/lib/api";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { SectionHeader } from "./ui/section-header";
import { Pill } from "./ui/pill";
import { InlineField } from "./ui/display";
import { MarkdownContent } from "./markdown-content";

// Renders a capability's display title: the human title when present, otherwise a
// fallback to its identifier rendered in monospace to signal it is an id, not a
// display title.
function CapabilityTitle({ title, identifier }: { title?: string | null; identifier: string }) {
  const display = (title ?? "").trim();
  if (display && display !== identifier) return <>{display}</>;
  return <Span fontFamily="var(--app-font-mono)" fontWeight="medium">{identifier}</Span>;
}

// MCP tool descriptions come from Python docstrings, whose Arguments:/
// Returns:/etc. sections duplicate the tool's input schema. For the
// capability browser we only show the human-readable summary above it.
const DOCSTRING_SECTION = /\n[ \t]*(Arguments|Parameters|Params|Returns|Yields|Raises|Examples?|Notes?|See Also|References|Todo|Warnings?)(\s*\([^)]*\))?\s*:/i;

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
// agent and rendered as collapsible rows, so you can see what an agent can do —
// plus the tools exposed by the configured MCP servers, grouped per server.
// Every row starts collapsed to keep the empty state uncluttered.
export function AgentSkills({ card, workingDirectory }: { card: AgentCard | null; workingDirectory?: string }) {
  const translation = useTranslations("AgentSkills");
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
  const skillsById = new Map<string, AgentSkill>();
  for (const skill of card?.skills ?? []) {
    skillsById.set(skill.id, skill);
  }
  for (const skill of folderSkills) {
    skillsById.set(skill.id, skill);
  }
  const skills = [...skillsById.values()].sort(disabledLast);
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

  return (
    <Box w="100%" maxW="640px" mx="auto" pb={4}>
      {hasSkills && (
        <>
          <SectionHeader
            icon={<LuListChecks size={14} />}
            title={translation("skillsAvailable")}
            description={translation("skillsDescription")}
          />
          <Flex direction="column" gap={2}>
            {/* One list, not two. Where a skill is defined is how it got here, not something
                anyone picks it by — splitting on it made every capability one level deeper
                and asked the reader to care about the filesystem. */}
            {[...globalSkills, ...projectSkills].map((skill) => <SkillCard key={skill.id} skill={skill} />)}
          </Flex>
        </>
      )}

      {hasTools && (
        <Box mt={hasSkills ? 6 : 0}>
          <SectionHeader
            icon={<LuWrench size={14} />}
            title={translation("toolsAvailable")}
            description={translation("toolsDescription")}
          />
          <Flex direction="column" gap={2}>
            {[...globalServers, ...projectServers].map((server) => <McpServerGroup key={server.name} server={server} />)}
          </Flex>
        </Box>
      )}
    </Box>
  );
}

// One agent skill, collapsed by default. A disabled skill is greyed out and inert;
// a skill with no description or examples is a plain, non-expanding line.
function SkillCard({ skill }: { skill: AgentSkill }) {
  const translation = useTranslations("AgentSkills");
  const enabled = skill.enabled !== false;
  const hasBody = !!skill.description || (skill.examples?.length ?? 0) > 0;
  return (
    <DisclosureRow
      disabled={!enabled}
      icon={<Box color="fg.muted"><LuPuzzle /></Box>}
      title={<DisclosureLabel><CapabilityTitle title={skill.title ?? skill.name} identifier={skill.id} /></DisclosureLabel>}
      badges={enabled ? undefined : <Pill colorPalette="gray">{translation("disabled")}</Pill>}
    >
      {enabled && hasBody ? (
        <>
          {skill.description && (
            <Box color="fg.muted">
              <MarkdownContent content={skill.description} fontSize="xs" />
            </Box>
          )}
          {skill.examples && skill.examples.length > 0 && (
            <Box mt={2}>
              <InlineField label={translation("examples")}>
                <Flex direction="column" gap={1}>
                  {skill.examples.map((example, index) => (
                    <Text key={index} fontSize="xs" color="fg.muted">“{example}”</Text>
                  ))}
                </Flex>
              </InlineField>
            </Box>
          )}
        </>
      ) : undefined}
    </DisclosureRow>
  );
}

// One MCP server's tools, collapsed by default. A disabled server is greyed out and
// inert; an enabled server shows its tool count and expands to the tool rows.
function McpServerGroup({ server }: { server: McpServerTools }) {
  const translation = useTranslations("AgentSkills");
  const enabled = server.enabled !== false;
  return (
    <DisclosureRow
      disabled={!enabled}
      icon={<Box color="fg.muted"><LuPlug /></Box>}
      title={<DisclosureLabel><CapabilityTitle identifier={server.name} /></DisclosureLabel>}
      badges={
        enabled
          ? <Pill colorPalette="gray">{translation("toolCount", { count: server.tools.length })}</Pill>
          : <Pill colorPalette="gray">{translation("disabled")}</Pill>
      }
    >
      {enabled && server.tools.length > 0 ? (
        <Flex direction="column" gap={2}>
          {server.tools.map((tool) => (
            <McpToolRow key={tool.name} tool={tool} />
          ))}
        </Flex>
      ) : undefined}
    </DisclosureRow>
  );
}

// A single MCP tool: its human title when present, else its name (id) in monospace.
function McpToolRow({ tool }: { tool: McpTool }) {
  return (
    <Box>
      <Text textStyle="fieldLabel">
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
