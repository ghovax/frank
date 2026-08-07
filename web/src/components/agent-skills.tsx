"use client";

import { Box, Flex, Span, Text } from "@chakra-ui/react";
import { swallowed } from "@/lib/swallowed";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
// The same glyphs the transcript uses for these things.
import { CONCEPT_ICONS } from "@/lib/glyphs";
import {
  fetchMcpTools,
  fetchSkills,
  subscribeEvents,
  type AgentCard,
  type AgentSkill,
  type McpServerTools,
  type McpTool,
} from "@/lib/api";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { SectionHeader } from "./ui/section-header";
import { Pill } from "./ui/pill";
import { InlineField } from "./ui/display";
import { MarkdownContent } from "./markdown-content";

// A capability's title, falling back to its identifier in monospace to signal it is an id.
function CapabilityTitle({ title, identifier }: { title?: string | null; identifier: string }) {
  const display = (title ?? "").trim();
  if (display && display !== identifier) return <>{display}</>;
  return (
    <Span fontFamily="var(--app-font-mono)" fontWeight="medium">
      {identifier}
    </Span>
  );
}

// Tool descriptions come from docstrings whose sections duplicate the input schema, so only the summary is shown.
const DOCSTRING_SECTION =
  /\n[ \t]*(Arguments|Args|Parameters|Params|Returns|Yields|Raises|Examples?|Notes?|See Also|References|Todo|Warnings?)(\s*\([^)]*\))?\s*:/i;

function docstringSummary(description: string): string {
  const match = description.match(DOCSTRING_SECTION);
  return match ? description.slice(0, match.index ?? 0).trim() : description.trim();
}

// Comparator that pushes disabled capabilities to the end while preserving relative order.
function disabledLast(first: { enabled?: boolean }, second: { enabled?: boolean }): number {
  return Number(first.enabled === false) - Number(second.enabled === false);
}

// The selected agent's skills as collapsible rows, plus the tools its configured servers expose.
export function AgentSkills({
  card,
  workingDirectory,
}: {
  card: AgentCard | null;
  workingDirectory?: string;
}) {
  const translation = useTranslations("AgentSkills");
  const [mcpServers, setMcpServers] = useState<McpServerTools[]>([]);
  const [folderSkills, setFolderSkills] = useState<AgentSkill[]>([]);

  useEffect(() => {
    let cancelled = false;
    // Skills and servers are scoped to the selected folder, so both refetch whenever it changes.
    const loadCapabilities = () => {
      fetchSkills(workingDirectory)
        .then((skills) => {
          if (!cancelled) setFolderSkills(skills);
        })
        .catch((caught) =>
          swallowed({ component: "agent-skills", operation: "list the skills" }, caught),
        );
      fetchMcpTools(workingDirectory)
        .then((servers) => {
          if (!cancelled) setMcpServers(servers);
        })
        .catch((caught) =>
          swallowed({ component: "agent-skills", operation: "list the MCP tools" }, caught),
        );
    };
    loadCapabilities();
    // Both reload live, since their files are watched, so refetch when the server signals a change.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") loadCapabilities();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [workingDirectory]);

  // Disabled capabilities are greyed and sorted to the bottom, stably, so they do not clutter the active ones.
  const skillsById = new Map<string, AgentSkill>();
  for (const skill of card?.skills ?? []) {
    skillsById.set(skill.id, skill);
  }
  for (const skill of folderSkills) {
    skillsById.set(skill.id, skill);
  }
  const skills = [...skillsById.values()].sort(disabledLast);
  const hasSkills = skills.length > 0;
  // Disabled servers are shown greyed; enabled ones still connecting stay hidden until they advertise something.
  const toolServers = mcpServers
    .filter((server) => server.enabled === false || server.tools.length > 0)
    .sort(disabledLast);
  const hasTools = toolServers.length > 0;
  if (!hasSkills && !hasTools) return null;

  // Split each list into the global capabilities and the ones the selected folder contributes.
  const globalSkills = skills.filter((skill) => skill.scope !== "workspace");
  const workspaceSkills = skills.filter((skill) => skill.scope === "workspace");
  const globalServers = toolServers.filter((server) => server.scope !== "workspace");
  const workspaceServers = toolServers.filter((server) => server.scope === "workspace");

  // The home folder has no workspace scope of its own, so its always-empty group is suppressed.

  return (
    // A flex item's default minimum is its content width, which a long identifier would widen the panel past.
    <Box w="100%" minW={0} maxW="100%" pb={4}>
      {hasSkills && (
        <>
          <SectionHeader
            icon={<CONCEPT_ICONS.skill size={14} />}
            title={translation("skillsAvailable")}
            description={translation("skillsDescription")}
          />
          <Flex direction="column" gap={2}>
            {/* One list rather than two, since where a skill is defined is how it got here rather than what it is. */}
            {[...globalSkills, ...workspaceSkills].map((skill) => (
              <SkillCard key={skill.id} skill={skill} />
            ))}
          </Flex>
        </>
      )}

      {hasTools && (
        <Box mt={hasSkills ? 6 : 0}>
          <SectionHeader
            icon={<CONCEPT_ICONS.mcp size={14} />}
            title={translation("toolsAvailable")}
            description={translation("toolsDescription")}
          />
          <Flex direction="column" gap={2}>
            {[...globalServers, ...workspaceServers].map((server) => (
              <McpServerGroup key={server.name} server={server} />
            ))}
          </Flex>
        </Box>
      )}
    </Box>
  );
}

// One agent skill, collapsed by default, with a disabled one greyed and inert.
function SkillCard({ skill }: { skill: AgentSkill }) {
  const translation = useTranslations("AgentSkills");
  const enabled = skill.enabled !== false;
  const hasBody = !!skill.description || (skill.examples?.length ?? 0) > 0;
  return (
    <DisclosureRow
      disabled={!enabled}
      icon={
        <Box color="fg.muted">
          <CONCEPT_ICONS.skill />
        </Box>
      }
      title={
        <DisclosureLabel>
          <CapabilityTitle title={skill.title ?? skill.name} identifier={skill.id} />
        </DisclosureLabel>
      }
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
                    <Text key={index} fontSize="xs" color="fg.muted">
                      “{example}”
                    </Text>
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

// One server's tools, collapsed by default, with a disabled one greyed and inert.
function McpServerGroup({ server }: { server: McpServerTools }) {
  const translation = useTranslations("AgentSkills");
  const enabled = server.enabled !== false;
  return (
    <DisclosureRow
      disabled={!enabled}
      icon={
        <Box color="fg.muted">
          <CONCEPT_ICONS.mcp />
        </Box>
      }
      title={
        <DisclosureLabel>
          <CapabilityTitle identifier={server.name} />
        </DisclosureLabel>
      }
      badges={
        enabled ? (
          <Pill colorPalette="gray">
            {translation("toolCount", { count: server.tools.length })}
          </Pill>
        ) : (
          <Pill colorPalette="gray">{translation("disabled")}</Pill>
        )
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
