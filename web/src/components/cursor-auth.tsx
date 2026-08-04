"use client";

import { Alert, Box, Button, Text } from "@chakra-ui/react";
import { swallowed } from "@/lib/swallowed";
import { useCallback, useEffect, useRef, useState } from "react";
import { LuLogIn, LuLogOut } from "react-icons/lu";
import { useTranslations } from "next-intl";
import {
  fetchCursorAuthStatus,
  signOutCursor,
  startCursorLogin,
  type CursorAuthStatus,
} from "@/lib/api";

/**
 * Sign-in control for the experimental `cursor` subscription provider. Shared by the
 * Settings dialog and the model picker, exactly as its ChatGPT counterpart is.
 *
 * The two controls look alike on purpose — one button carrying the state, signed out
 * offering sign-in and signed in offering its reverse — because from here the two
 * subscriptions are the same idea, and only the server knows how differently they are
 * reached. Two differences do surface. Cursor's flow has no redirect, so nothing lands
 * back on this machine and there is no callback port to collide over: a sign-in cannot
 * fail at the start, only complete or time out. And there are no usage meters, because
 * Cursor reports no remaining allowance to a client — it says so only by refusing a turn.
 *
 * Completion is observed by polling: the frank daemon polls Cursor, and this polls the
 * daemon.
 */
export function CursorAuthControl({
  onStatusChange,
}: {
  onStatusChange?: (status: CursorAuthStatus) => void;
}) {
  const translation = useTranslations("CursorAuthControl");
  const [status, setStatus] = useState<CursorAuthStatus | null>(null);
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
    const next = await fetchCursorAuthStatus();
    setStatus(next);
    onStatusChange?.(next);
    return next;
  }, [onStatusChange]);

  useEffect(() => {
    let cancelled = false;
    fetchCursorAuthStatus()
      .then((next) => {
        if (cancelled) return;
        setStatus(next);
        onStatusChange?.(next);
      })
      .catch((caught) => swallowed({ component: "cursor-auth", operation: "read the Cursor sign-in state" }, caught));
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [onStatusChange, stopPolling]);

  async function handleSignIn() {
    setBusy(true);
    setError("");
    try {
      const { authorize_url } = await startCursorLogin();
      window.open(authorize_url, "_blank", "noopener,noreferrer");
      setAwaiting(true);
      const startedAt = Date.now();
      stopPolling();
      // The server's own poll window is a few minutes wide; give up watching at the same
      // point rather than leaving a spinner running against a flow that has already ended.
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
      await signOutCursor();
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
        <Alert.Root status="success" size="sm" borderRadius="md" mt={3} alignItems="center">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs" truncate>
              {status?.account ? translation("signedInAs", { account: status.account }) : translation("signedIn")}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
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
