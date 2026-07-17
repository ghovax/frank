"use client";

import { Button, Flex, Input, Text } from "@chakra-ui/react";
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
import { toaster } from "./ui/toaster";

type Draft = {
  name: string;
  cardUrl: string;
  authType: string;
  token: string;
  tokenUrl: string;
  clientId: string;
  clientSecret: string;
  allowPrivate: boolean;
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
  allowPrivate: false,
  allowedProfiles: "",
};

const HEALTH_COLOR: Record<string, string> = {
  ok: "green.500",
  unreachable: "orange.500",
  untrusted: "red.500",
  unresolved: "gray.500",
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
    allowPrivate: draft.allowPrivate,
    allowedProfiles: draft.allowedProfiles
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  };
}

// The external-agent manager inside Settings. Lists registered remote A2A agents with a
// live health dot, and an inline form to add one. Secrets are write-only (never returned
// by the server) — re-enter a token when changing it. remote-agents.json is the source of
// truth, so hand-editing that file works too.
export function RemoteAgentsPanel() {
  const [agents, setAgents] = useState<RemoteAgent[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    try {
      setAgents(await listRemoteAgents());
    } catch (error) {
      toaster.create({ title: "Could not load remote agents", type: "error" });
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
    } catch (error) {
      toaster.create({ title: "Could not save remote agent", type: "error" });
    } finally {
      setSaving(false);
    }
  }, [draft, reload]);

  const remove = useCallback(
    async (name: string) => {
      try {
        await deleteRemoteAgent(name);
        await reload();
      } catch (error) {
        toaster.create({ title: "Could not remove remote agent", type: "error" });
      }
    },
    [reload],
  );

  const refresh = useCallback(async (name: string) => {
    try {
      const result = await refreshRemoteAgent(name);
      toaster.create({ title: `${name}: ${result.health}`, type: result.health === "ok" ? "success" : "error" });
    } catch (error) {
      toaster.create({ title: "Could not refresh remote agent", type: "error" });
    }
  }, []);

  return (
    <Flex direction="column" gap={4}>
      <Flex direction="column" gap={2}>
        {agents.length === 0 && (
          <Text fontSize="sm" color="fg.muted">
            No external agents registered. Add one below, or edit ~/.agents/remote-agents.json.
          </Text>
        )}
        {agents.map((agent) => (
          <Flex
            key={agent.name}
            align="center"
            justify="space-between"
            gap={3}
            borderWidth="1px"
            borderRadius="md"
            px={3}
            py={2}
          >
            <Flex direction="column" gap={0} minW={0}>
              <Flex align="center" gap={2}>
                <span
                  title={agent.health + (agent.error ? `: ${agent.error}` : "")}
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: `var(--chakra-colors-${(HEALTH_COLOR[agent.health] ?? "gray.500").replace(".", "-")})`,
                    flex: "0 0 auto",
                  }}
                />
                <Text fontWeight="medium">{agent.name}</Text>
                {agent.resolvedName && agent.resolvedName !== agent.name && (
                  <Text fontSize="xs" color="fg.muted">
                    ({agent.resolvedName})
                  </Text>
                )}
              </Flex>
              <Text fontSize="xs" color="fg.muted" truncate>
                {agent.cardUrl}
              </Text>
              {agent.error && (
                <Text fontSize="xs" color="red.500" truncate>
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
        <Input
          size="sm"
          placeholder="Local name (e.g. acme-researcher)"
          value={draft.name}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
        />
        <Input
          size="sm"
          placeholder="Agent card URL (https://.../.well-known/agent-card.json)"
          value={draft.cardUrl}
          onChange={(event) => setDraft({ ...draft, cardUrl: event.target.value })}
        />
        <select
          value={draft.authType}
          onChange={(event) => setDraft({ ...draft, authType: event.target.value })}
          style={{ padding: "6px 8px", borderRadius: 6, borderWidth: 1, fontSize: 14 }}
        >
          <option value="none">No auth</option>
          <option value="bearer">Bearer token</option>
          <option value="api_key">API key header</option>
          <option value="oauth2">OAuth2 client credentials</option>
        </select>
        {(draft.authType === "bearer" || draft.authType === "api_key") && (
          <Input
            size="sm"
            placeholder="Token (or ${ENV_VAR})"
            value={draft.token}
            onChange={(event) => setDraft({ ...draft, token: event.target.value })}
          />
        )}
        {draft.authType === "oauth2" && (
          <>
            <Input size="sm" placeholder="Token URL" value={draft.tokenUrl} onChange={(event) => setDraft({ ...draft, tokenUrl: event.target.value })} />
            <Input size="sm" placeholder="Client ID" value={draft.clientId} onChange={(event) => setDraft({ ...draft, clientId: event.target.value })} />
            <Input size="sm" placeholder="Client secret (or ${ENV_VAR})" value={draft.clientSecret} onChange={(event) => setDraft({ ...draft, clientSecret: event.target.value })} />
          </>
        )}
        <Input
          size="sm"
          placeholder="Allowed profiles (comma-separated; blank = all)"
          value={draft.allowedProfiles}
          onChange={(event) => setDraft({ ...draft, allowedProfiles: event.target.value })}
        />
        <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={draft.allowPrivate}
            onChange={(event) => setDraft({ ...draft, allowPrivate: event.target.checked })}
          />
          Allow private/loopback address (e.g. localhost)
        </label>
        <Flex justify="flex-end">
          <Button size="sm" onClick={() => void save()} loading={saving}>
            Add agent
          </Button>
        </Flex>
      </Flex>
    </Flex>
  );
}
