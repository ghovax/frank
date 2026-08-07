"use client";

import { Button, Flex, IconButton, Input, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { LuPlus, LuRefreshCw, LuTrash2 } from "react-icons/lu";
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
import { SimpleSelect, type SelectOption } from "./ui/simple-select";
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

const AUTH_ITEMS: SelectOption[] = [
  { value: "none", label: "No auth" },
  { value: "bearer", label: "Bearer token" },
  { value: "api_key", label: "API key header" },
  { value: "oauth2", label: "OAuth2 client credentials" },
];

const YES_NO: SelectOption[] = [
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
      ? {
          type: "oauth2",
          tokenUrl: draft.tokenUrl,
          clientId: draft.clientId,
          clientSecret: draft.clientSecret,
        }
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

// Agents on other hosts, listed with a health pill; secrets are write-only and the file is the truth.
export function RemoteAgentsPanel() {
  const [agents, setAgents] = useState<RemoteAgent[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    try {
      setAgents(await listRemoteAgents());
    } catch {
      // The toast below is the report; a console copy would duplicate it.
      toaster.create({ title: "Could not load external agents", type: "error" });
    }
  }, []);

  // Read once, then follow the daemon, awaited so a closed panel can drop the answer.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const loaded = await listRemoteAgents();
        if (!cancelled) setAgents(loaded);
      } catch {
        // Reported by the toast below.
        if (!cancelled) toaster.create({ title: "Could not load external agents", type: "error" });
      }
    })();
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "remote_agents_changed") void reload();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
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
      // Reported by the toast below.
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
        // Reported by the toast below.
        toaster.create({ title: "Could not remove external agent", type: "error" });
      }
    },
    [reload],
  );

  const refresh = useCallback(async (name: string) => {
    try {
      const result = await refreshRemoteAgent(name);
      toaster.create({
        title: `${name}: ${result.health}`,
        type: result.health === "ok" ? "success" : "error",
      });
    } catch {
      // Reported by the toast below.
      toaster.create({ title: "Could not refresh external agent", type: "error" });
    }
  }, []);

  const update = (patch: Partial<Draft>) => setDraft((current) => ({ ...current, ...patch }));

  return (
    <Flex direction="column" gap={3}>
      {agents.length === 0 ? (
        <Text fontSize="sm" color="fg.muted">
          No external agents registered. Add one below, or edit ~/.agents/remote-agents.json.
        </Text>
      ) : (
        agents.map((agent) => (
          <Flex
            key={agent.name}
            align="center"
            justify="space-between"
            gap={3}
            borderWidth="1px"
            borderColor="border"
            borderRadius="md"
            p={3}
          >
            <Flex direction="column" gap={1} minW={0}>
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
            <Flex gap={1} flexShrink={0}>
              <IconButton
                aria-label="Refresh card"
                variant="ghost"
                onClick={() => void refresh(agent.name)}
              >
                <LuRefreshCw size={13} />
              </IconButton>
              <IconButton
                aria-label="Remove agent"
                variant="ghost"
                colorPalette="red"
                onClick={() => void remove(agent.name)}
              >
                <LuTrash2 size={13} />
              </IconButton>
            </Flex>
          </Flex>
        ))
      )}

      <Flex
        direction="column"
        gap={3}
        borderWidth="1px"
        borderColor="border"
        borderRadius="md"
        p={3}
      >
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">Name</Text>
          <Input
            value={draft.name}
            onChange={(event) => update({ name: event.target.value })}
            placeholder="acme-researcher"
          />
        </Flex>
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">Agent card URL</Text>
          <Input
            value={draft.cardUrl}
            onChange={(event) => update({ cardUrl: event.target.value })}
            placeholder="https://agents.example.com/.well-known/agent-card.json"
          />
        </Flex>
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">Authentication</Text>
          <SimpleSelect
            items={AUTH_ITEMS}
            value={draft.authType}
            onValueChange={(value) => update({ authType: value })}
          />
        </Flex>
        {(draft.authType === "bearer" || draft.authType === "api_key") && (
          <Flex direction="column" gap={1}>
            <Text textStyle="fieldLabel">Token</Text>
            <Input
              value={draft.token}
              onChange={(event) => update({ token: event.target.value })}
              placeholder="${ACME_TOKEN}"
            />
          </Flex>
        )}
        {draft.authType === "oauth2" && (
          <>
            <Flex direction="column" gap={1}>
              <Text textStyle="fieldLabel">Token URL</Text>
              <Input
                value={draft.tokenUrl}
                onChange={(event) => update({ tokenUrl: event.target.value })}
                placeholder="https://auth.example.com/oauth/token"
              />
            </Flex>
            <Flex direction="column" gap={1}>
              <Text textStyle="fieldLabel">Client ID</Text>
              <Input
                value={draft.clientId}
                onChange={(event) => update({ clientId: event.target.value })}
              />
            </Flex>
            <Flex direction="column" gap={1}>
              <Text textStyle="fieldLabel">Client secret</Text>
              <Input
                value={draft.clientSecret}
                onChange={(event) => update({ clientSecret: event.target.value })}
                placeholder="${ACME_CLIENT_SECRET}"
              />
            </Flex>
          </>
        )}
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">Allowed profiles</Text>
          <Input
            value={draft.allowedProfiles}
            onChange={(event) => update({ allowedProfiles: event.target.value })}
            placeholder="comma-separated; blank = all"
          />
        </Flex>
        <Flex direction="column" gap={1}>
          <Text textStyle="fieldLabel">Allow private/loopback host</Text>
          <SimpleSelect
            items={YES_NO}
            value={draft.allowPrivate}
            onValueChange={(value) => update({ allowPrivate: value })}
          />
        </Flex>
        <Button
          variant="subtle"
          colorPalette="blue"
          w="100%"
          loading={saving}
          onClick={() => void save()}
        >
          <LuPlus size={14} /> Add external agent
        </Button>
      </Flex>
    </Flex>
  );
}
