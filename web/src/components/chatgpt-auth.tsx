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

/**
 * Sign-in control for the experimental `chatgpt` subscription provider. Shared by
 * the Settings dialog and the model picker (the "both" surfaces).
 *
 * A single button carries the state: signed out shows "Sign in with ChatGPT";
 * signed in shows "Sign out" (with the account) and reverses it. Sign-in opens
 * OpenAI's authorize URL in a browser; the daisy server catches the loopback
 * redirect and persists the token, which we then observe by polling.
 */
export function ChatGPTAuthControl({
  onStatusChange,
}: {
  onStatusChange?: (status: ChatGPTAuthStatus) => void;
}) {
  const t = useTranslations("ChatGPTAuthControl");
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
      setError((caught as Error).message || t("signInError"));
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
        {t("title")}
      </Text>
      {signedIn ? (
        <Button
          colorPalette="red"
          onClick={handleSignOut}
          loading={busy}
        >
          <LuLogOut size={14} />
          {t("signOut")}
        </Button>
      ) : (
        <Button
          colorPalette="green"
          onClick={handleSignIn}
          loading={busy || awaiting}
          loadingText={awaiting ? t("waitingForBrowser") : undefined}
        >
          <LuLogIn size={14} />
          {t("signIn")}
        </Button>
      )}
      {signedIn ? (
        <Alert.Root status="success" size="sm" borderRadius="md" mt={3} alignItems="center">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs" truncate>
              {status?.email ? t("signedInAs", { email: status.email }) : t("signedIn")}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      ) : (
        <Alert.Root status="info" size="sm" borderRadius="md" mt={3} alignItems="center">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs">
              {t("planNotice")}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      )}
    </Box>
  );
}
