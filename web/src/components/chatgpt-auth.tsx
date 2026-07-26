"use client";

import { Alert, Box, Button, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { LuLogIn, LuLogOut } from "react-icons/lu";
import { useTranslations } from "next-intl";
import {
  fetchChatGPTAuthStatus,
  signOutChatGPT,
  startChatGPTLogin,
  type ChatGPTAuthStatus,
} from "@/lib/api";
import { ChatGPTUsageMeters } from "./chatgpt-usage-meters";

/**
 * Sign-in control for the experimental `chatgpt` subscription provider. Shared by
 * the Settings dialog and the model picker (the "both" surfaces).
 *
 * A single button carries the state: signed out shows "Sign in with ChatGPT";
 * signed in shows "Sign out" (with the account) and reverses it. Sign-in opens
 * OpenAI's authorize URL in a browser; the daisy daemon catches the loopback
 * redirect and persists the token, which we then observe by polling.
 */
export function ChatGPTAuthControl({
  onStatusChange,
}: {
  onStatusChange?: (status: ChatGPTAuthStatus) => void;
}) {
  const translation = useTranslations("ChatGPTAuthControl");
  const [status, setStatus] = useState<ChatGPTAuthStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [awaiting, setAwaiting] = useState(false);
  const [error, setError] = useState("");
  const pollRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    const next = await fetchChatGPTAuthStatus();
    setStatus(next);
    onStatusChange?.(next);
    return next;
  }, [onStatusChange]);

  useEffect(() => {
    let cancelled = false;
    fetchChatGPTAuthStatus()
      .then((next) => {
        if (cancelled) return;
        setStatus(next);
        onStatusChange?.(next);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [onStatusChange, stopPolling]);

  // Keep the usage meters fresh while the viewer is open. The server re-snapshots the
  // account's limits from the headers on every turn, so a turn's send/receive is
  // always captured server-side; this light re-poll is only so an open viewer reflects
  // it without a dedicated event channel. Runs only while signed in; unmount stops it.
  useEffect(() => {
    if (!status?.signed_in) return;
    const id = window.setInterval(() => {
      refresh().catch(() => {});
    }, 5000);
    return () => window.clearInterval(id);
  }, [status?.signed_in, refresh]);

  async function handleSignIn() {
    setBusy(true);
    setError("");
    try {
      const { authorize_url } = await startChatGPTLogin();
      window.open(authorize_url, "_blank", "noopener,noreferrer");
      setAwaiting(true);
      const startedAt = Date.now();
      stopPolling();
      pollRef.current = window.setInterval(async () => {
        const next = await refresh().catch(() => null);
        if (next?.signed_in || Date.now() - startedAt > 300_000) {
          stopPolling();
          setAwaiting(false);
        }
      }, 2000);
    } catch (caught) {
      setError((caught as Error).message || translation("signInError"));
    } finally {
      setBusy(false);
    }
  }

  async function handleSignOut() {
    setBusy(true);
    setError("");
    stopPolling();
    setAwaiting(false);
    try {
      await signOutChatGPT();
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  const signedIn = !!status?.signed_in;

  return (
    <Box>
      <Text textStyle="fieldLabel" mb={1.5}>
        {translation("title")}
      </Text>
      {signedIn ? (
        <Button
          colorPalette="red"
          onClick={handleSignOut}
          loading={busy}
        >
          <LuLogOut size={14} />
          {translation("signOut")}
        </Button>
      ) : (
        <Button
          colorPalette="green"
          onClick={handleSignIn}
          loading={busy || awaiting}
          loadingText={awaiting ? translation("waitingForBrowser") : undefined}
        >
          <LuLogIn size={14} />
          {translation("signIn")}
        </Button>
      )}
      {error && (
        <Alert.Root status="error" size="sm" borderRadius="md" mt={3} alignItems="center">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs" truncate title={error}>
              {error}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      )}
      {signedIn ? (
        <>
          <Alert.Root status="success" size="sm" borderRadius="md" mt={3} alignItems="center">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Description fontSize="xs" truncate>
                {status?.email ? translation("signedInAs", { email: status.email }) : translation("signedIn")}
              </Alert.Description>
            </Alert.Content>
          </Alert.Root>
          {(status?.usage?.windows?.length ?? 0) > 0 && (
            <Box mt={3}>
              <ChatGPTUsageMeters usage={status?.usage ?? null} />
            </Box>
          )}
        </>
      ) : (
        <Alert.Root status="info" size="sm" borderRadius="md" mt={3} alignItems="center">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs">
              {translation("planNotice")}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      )}
    </Box>
  );
}
