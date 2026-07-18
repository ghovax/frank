"use client";

import { Box, Button, Flex, Input, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import {
  deleteRemoteAgent,
  listRemoteAgents,
  refreshRemoteAgent,
  subscribeEvents,
  upsertRemoteAgent,
  type RemoteAgent,
  type RemoteAgentInput,
} from "@/lib/api";
import { Pill } from "./ui/pill";
import { SimpleSelect } from "./ui/simple-select";
import { toaster } from "./ui/toaster";

type Draft = {
  name: string;
  cardUrl: string;
  authType: string;
  token: string;
  tokenUrl: string;
  clientId: string;
  clientSecret: string;
  allowPrivate: string;
  allowedProfiles: string;
};

const EMPTY_DRAFT: Draft = {
  name: "",
  cardUrl: "",
  authType: "none",
  token: "",
  tokenUrl: "",
  clientId: "",
  clientSecret: "",
  allowPrivate: "no",
  allowedProfiles: "",
};

const AUTH_ITEMS = [
  { value: "none", label: "No auth" },
  { value: "bearer", label: "Bearer token" },
  { value: "api_key", label: "API key header" },
  { value: "oauth2", label: "OAuth2 client credentials" },
];

const YES_NO = [
  { value: "no", label: "No" },
  { value: "yes", label: "Yes" },
];

const HEALTH_PALETTE: Record<string, string> = {
  ok: "green",
  unreachable: "orange",
  untrusted: "red",
  unresolved: "gray",
};

function draftToInput(draft: Draft): RemoteAgentInput {
  const auth =
    draft.authType === "oauth2"
      ? { type: "oauth2", tokenUrl: draft.tokenUrl, clientId: draft.clientId, clientSecret: draft.clientSecret }
      : draft.authType === "none"
        ? { type: "none" }
        : { type: draft.authType, token: draft.token };
  return {
    name: draft.name.trim(),
    cardUrl: draft.cardUrl.trim(),
    enabled: true,
    auth,
    allowPrivate: draft.allowPrivate === "yes",
    allowedProfiles: draft.allowedProfiles
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  };
}

// External A2A agents this harness can delegate to. Lists the registered agents with a
// live health pill and an inline form to add one. Secrets are write-only (never returned),
// and remote-agents.json is the source of truth, so hand-editing that file works too.
export function RemoteAgentsPanel() {
  const [agents, setAgents] = useState<RemoteAgent[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    try {
      setAgents(await listRemoteAgents());
    } catch {
      toaster.create({ title: "Could not load external agents", type: "error" });
    }
  }, []);

  useEffect(() => {
    void reload();
    return subscribeEvents((event) => {
      if (event.type === "remote_agents_changed") void reload();
    });
  }, [reload]);

  const save = useCallback(async () => {
    const input = draftToInput(draft);
    if (!input.name || !input.cardUrl) {
      toaster.create({ title: "Name and card URL are required", type: "error" });
      return;
    }
    setSaving(true);
    try {
      await upsertRemoteAgent(input);
      setDraft(EMPTY_DRAFT);
      await reload();
      toaster.create({ title: `Saved ${input.name}`, type: "success" });
    } catch {
      toaster.create({ title: "Could not save external agent", type: "error" });
    } finally {
      setSaving(false);
    }
  }, [draft, reload]);

  const remove = useCallback(
    async (name: string) => {
      try {
        await deleteRemoteAgent(name);
        await reload();
      } catch {
        toaster.create({ title: "Could not remove external agent", type: "error" });
      }
    },
    [reload],
  );

  const refresh = useCallback(async (name: string) => {
    try {
      const result = await refreshRemoteAgent(name);
      toaster.create({ title: `${name}: ${result.health}`, type: result.health === "ok" ? "success" : "error" });
    } catch {
      toaster.create({ title: "Could not refresh external agent", type: "error" });
    }
  }, []);

  const update = (patch: Partial<Draft>) => setDraft((current) => ({ ...current, ...patch }));

  return (
    <Flex direction="column" gap={4}>
      <Flex direction="column" gap={2}>
        {agents.length === 0 && (
          <Text fontSize="sm" color="fg.muted">
            No external agents registered. Add one below, or edit ~/.agents/remote-agents.json.
          </Text>
        )}
        {agents.map((agent) => (
          <Flex key={agent.name} align="center" justify="space-between" gap={3} borderWidth="1px" borderRadius="md" px={3} py={2}>
            <Flex direction="column" gap={0.5} minW={0}>
              <Flex align="center" gap={2}>
                <Pill colorPalette={HEALTH_PALETTE[agent.health] ?? "gray"}>{agent.health}</Pill>
                <Text fontWeight="medium">{agent.name}</Text>
                {agent.resolvedName && agent.resolvedName !== agent.name && (
                  <Text fontSize="xs" color="fg.muted">
                    {agent.resolvedName}
                  </Text>
                )}
              </Flex>
              <Text fontSize="xs" color="fg.muted" truncate>
                {agent.cardUrl}
              </Text>
              {agent.error && (
                <Text fontSize="xs" color="red.fg" truncate>
                  {agent.error}
                </Text>
              )}
            </Flex>
            <Flex gap={2} flex="0 0 auto">
              <Button size="xs" variant="outline" onClick={() => void refresh(agent.name)}>
                Refresh
              </Button>
              <Button size="xs" variant="outline" colorPalette="red" onClick={() => void remove(agent.name)}>
                Remove
              </Button>
            </Flex>
          </Flex>
        ))}
      </Flex>

      <Flex direction="column" gap={2} borderWidth="1px" borderRadius="md" p={3}>
        <Text fontWeight="medium" fontSize="sm">
          Add external agent
        </Text>
        <Input size="xs" placeholder="Local name (e.g. acme-researcher)" value={draft.name} onChange={(event) => update({ name: event.target.value })} />
        <Input
          size="xs"
          placeholder="Agent card URL (https://.../.well-known/agent-card.json)"
          value={draft.cardUrl}
          onChange={(event) => update({ cardUrl: event.target.value })}
        />
        <Box w="240px">
          <SimpleSelect items={AUTH_ITEMS} value={draft.authType} onValueChange={(value) => update({ authType: value })} />
        </Box>
        {(draft.authType === "bearer" || draft.authType === "api_key") && (
          <Input size="xs" placeholder="Token (or ${ENV_VAR})" value={draft.token} onChange={(event) => update({ token: event.target.value })} />
        )}
        {draft.authType === "oauth2" && (
          <>
            <Input size="xs" placeholder="Token URL" value={draft.tokenUrl} onChange={(event) => update({ tokenUrl: event.target.value })} />
            <Input size="xs" placeholder="Client ID" value={draft.clientId} onChange={(event) => update({ clientId: event.target.value })} />
            <Input size="xs" placeholder="Client secret (or ${ENV_VAR})" value={draft.clientSecret} onChange={(event) => update({ clientSecret: event.target.value })} />
          </>
        )}
        <Input
          size="xs"
          placeholder="Allowed profiles (comma-separated; blank = all)"
          value={draft.allowedProfiles}
          onChange={(event) => update({ allowedProfiles: event.target.value })}
        />
        <Flex align="center" justify="space-between" gap={3}>
          <Flex align="center" gap={2}>
            <Text fontSize="xs" color="fg.muted">
              Allow private/loopback host
            </Text>
            <Box w="90px">
              <SimpleSelect items={YES_NO} value={draft.allowPrivate} onValueChange={(value) => update({ allowPrivate: value })} />
            </Box>
          </Flex>
          <Button size="xs" onClick={() => void save()} loading={saving}>
            Add agent
          </Button>
        </Flex>
      </Flex>
    </Flex>
  );
}
