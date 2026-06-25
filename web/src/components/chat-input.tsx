"use client";

import {
  Box,
  createListCollection,
  Flex,
  IconButton,
  Input,
  Portal,
  Select,
  Text,
  VStack,
} from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { LuArrowUp, LuSquare } from "react-icons/lu";

interface SlashCommand {
  name: string;
  description: string;
}

const SLASH_COMMANDS: SlashCommand[] = [
  { name: "/new", description: "Start a new conversation" },
  { name: "/agent", description: "Switch to a different agent" },
  { name: "/abort", description: "Stop the current response" },
  { name: "/clear", description: "Clear the conversation" },
  { name: "/history", description: "Show conversation history" },
];

interface ChatInputProps {
  onSend: (text: string) => void;
  onAbort: () => void;
  onSlashCommand?: (command: string) => void;
  isStreaming: boolean;
  disabled?: boolean;
  agents: string[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
}

export function ChatInput({
  onSend,
  onAbort,
  onSlashCommand,
  isStreaming,
  disabled,
  agents,
  selectedAgent,
  onAgentChange,
}: ChatInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [inputValue, setInputValue] = useState("");
  const [showCommands, setShowCommands] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);

  const agentCollection = useMemo(
    () =>
      createListCollection({
        items: agents.map((agentName) => ({
          label: agentName,
          value: agentName,
        })),
      }),
    [agents]
  );

  const filteredCommands = inputValue.startsWith("/")
    ? SLASH_COMMANDS.filter((command) =>
        command.name.startsWith(inputValue.toLowerCase())
      )
    : [];

  useEffect(() => {
    setShowCommands(filteredCommands.length > 0 && inputValue.startsWith("/"));
    setSelectedCommandIndex(0);
  }, [inputValue, filteredCommands.length]);

  function handleSubmit() {
    const trimmed = inputValue.trim();
    if (!trimmed) return;

    if (trimmed.startsWith("/")) {
      onSlashCommand?.(trimmed);
      setInputValue("");
      setShowCommands(false);
      return;
    }

    onSend(trimmed);
    setInputValue("");
  }

  function handleSelectCommand(command: SlashCommand) {
    if (command.name === "/agent") {
      setInputValue("/agent ");
      inputRef.current?.focus();
      return;
    }
    onSlashCommand?.(command.name);
    setInputValue("");
    setShowCommands(false);
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (showCommands) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setSelectedCommandIndex((current) =>
          current < filteredCommands.length - 1 ? current + 1 : 0
        );
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setSelectedCommandIndex((current) =>
          current > 0 ? current - 1 : filteredCommands.length - 1
        );
        return;
      }
      if (event.key === "Tab" || (event.key === "Enter" && filteredCommands.length > 0)) {
        event.preventDefault();
        handleSelectCommand(filteredCommands[selectedCommandIndex]);
        return;
      }
      if (event.key === "Escape") {
        setShowCommands(false);
        return;
      }
    }

    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (isStreaming) return;
      handleSubmit();
    }
  }

  return (
    <Box position="relative" borderTop="1px solid" borderColor="border" bg="bg.subtle">
      {showCommands && (
        <Box
          position="absolute"
          bottom="100%"
          left={3}
          right={3}
          mb={1}
          bg="bg.panel"
          border="1px solid"
          borderColor="border"
          borderRadius="lg"
          boxShadow="md"
          overflow="hidden"
        >
          <VStack gap={0} align="stretch">
            {filteredCommands.map((command, index) => (
              <Flex
                key={command.name}
                align="center"
                gap={3}
                px={3}
                py={1.5}
                cursor="pointer"
                bg={index === selectedCommandIndex ? "bg.emphasized" : undefined}
                _hover={{ bg: "bg.muted" }}
                onClick={() => handleSelectCommand(command)}
              >
                <Text fontSize="sm" fontWeight="medium">{command.name}</Text>
                <Text fontSize="xs" color="fg.muted">{command.description}</Text>
              </Flex>
            ))}
          </VStack>
        </Box>
      )}

      <Flex gap={2} p={3} align="center">
        <Select.Root
          collection={agentCollection}
          value={[selectedAgent]}
          onValueChange={(details) => {
            if (details.value[0]) onAgentChange(details.value[0]);
          }}
          size="sm"
          w="110px"
        >
          <Select.Control>
            <Select.Trigger borderRadius="lg" fontSize="sm">
              <Select.ValueText placeholder="Agent" />
            </Select.Trigger>
            <Select.IndicatorGroup>
              <Select.Indicator />
            </Select.IndicatorGroup>
          </Select.Control>
          <Portal>
            <Select.Positioner>
              <Select.Content borderRadius="lg">
                {agentCollection.items.map((item) => (
                  <Select.Item item={item} key={item.value}>
                    {item.label}
                    <Select.ItemIndicator />
                  </Select.Item>
                ))}
              </Select.Content>
            </Select.Positioner>
          </Portal>
        </Select.Root>

        <Box position="relative" flex={1} bg="bg" borderRadius="lg" border="1px solid" borderColor="border">
          <Input
            ref={inputRef}
            placeholder={disabled ? "Connecting to server..." : "Send a message or type / for commands..."}
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isStreaming || disabled}
            fontSize="sm"
            size="sm"
            h="34px"
            border="none"
            _focus={{ outline: "none", boxShadow: "none" }}
            pr="36px"
          />
          <Box position="absolute" right="5px" top="50%" transform="translateY(-50%)">
            {isStreaming ? (
              <IconButton
                aria-label="Stop"
                onClick={onAbort}
                colorPalette="red"
                variant="ghost"
                size="sm"
                borderRadius="md"
                minW="26px"
                h="26px"
                p={0}
              >
                <LuSquare />
              </IconButton>
            ) : (
              <IconButton
                aria-label="Send"
                onClick={handleSubmit}
                colorPalette="blue"
                variant="ghost"
                size="sm"
                borderRadius="md"
                minW="26px"
                h="26px"
                p={0}
                disabled={disabled}
              >
                <LuArrowUp />
              </IconButton>
            )}
          </Box>
        </Box>
      </Flex>
    </Box>
  );
}
