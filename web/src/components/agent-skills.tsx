"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { LuListChecks, LuWrench } from "react-icons/lu";
import type { AgentCard } from "@/lib/api";
import { ToolCard, ToolCardHeader, ToolMetaRow } from "./tool-card";

// Shows the selected agent's A2A AgentCard skills — broadcast from the served
// agent and rendered as cards, so you can see what an agent can do when loaded.
export function AgentSkills({ card }: { card: AgentCard | null }) {
  if (!card || card.skills.length === 0) return null;
  return (
    <Box w="100%" maxW="640px" mx="auto">
      <Flex align="center" gap={1.5} mb={2} color="fg.muted">
        <LuListChecks size={13} />
        <Text fontSize="xs" fontWeight="bold">Available capabilities</Text>
      </Flex>
      {card.description && (
        <Text fontSize="xs" color="fg.muted" mb={2}>{card.description}</Text>
      )}
      <Flex direction="column" gap={2}>
        {card.skills.map((skill) => {
          return (
            <ToolCard key={skill.id}>
              <ToolCardHeader
                icon={<Box color="fg.muted"><LuWrench size={12} /></Box>}
                title={skill.name || skill.id}
              />
              <Box px={2} py={2} borderTop="1px solid" borderColor="border" bg="bg">
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
              </Box>
            </ToolCard>
          );
        })}
      </Flex>
    </Box>
  );
}
