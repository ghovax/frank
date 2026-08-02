"use client";

import {
  Box,
  Button,
  Flex,
  IconButton,
  Input,
  Separator,
  Spinner,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";
import { LuArrowUp, LuCoins, LuFoldVertical, LuMic, LuMicOff, LuPaperclip, LuSquare } from "react-icons/lu";
import { fetchChatGPTAuthStatus, fetchDictationStatus, type DictationState, fetchMessageHistory, referenceAttachment, saveMessageHistory, subscribeEvents, uploadFile, type Attachment, type ChatGPTUsage, type ModelOption, type PermissionMode, type ProviderOption, type SandboxEnforce } from "@/lib/api";
import { DictationRecordingError, startDictation, type Dictation } from "@/lib/dictation";
import { toaster } from "./ui/toaster";
import { ChatGPTUsageMeters } from "./chatgpt-usage-meters";
import { AgentSelectControl, PermissionModeControl, SandboxToggleControl } from "./session-controls";
import { isTauri } from "@/lib/app-state";
import { pickDesktopFilePaths, watchDesktopFileDrop } from "@/lib/desktop-files";
import { AttachmentChip } from "./attachment-chips";
import { Tooltip } from "./ui/tooltip";
import { ConfirmDialog } from "./ui/confirm-dialog";
import { ModelSelect, modelSupportsVision } from "./model-select";
// SettingsDialog moved to ChatPanel top bar
import type { TokenUsage } from "@/lib/use-chat";
import { InlineField } from "./ui/display";
import { Strong } from "./ui/semantic";
import { swallowed } from "@/lib/swallowed";
import { errorMessage } from "@/lib/errors";

interface ChatInputProps {
  // Returns the session id when the send created one, which the composer ignores — it is
  // the caller's business, not the input's.
  onSend: (text: string, dataParts?: Record<string, unknown>[]) => void | Promise<void | string>;
  onAbort: () => void | Promise<void>;
  isStreaming: boolean;
  disabled?: boolean;
  sessionId?: string | null;
  initialDraft?: string;
  onDraftChange?: (draft: string) => void;
  workingDirectory?: string;
  // Whether the working directory is a valid path (resolved once at the workspace
  // level). Gates send and colours the composer border; the composer no longer
  // validates the directory itself.
  directoryValid?: boolean;
  agents: { id: string; name: string; title?: string; description?: string }[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
  models: ModelOption[];
  modelProviders: ProviderOption[];
  recentModels?: { id: string; name: string; provider: string }[];
  // The active agent's configured model identifier (provider/model). Drives the
  // composer chip and the attachment/vision gates. The chip reconfigures the
  // active agent's model via onAgentModelChange, so a change affects every
  // session running that agent — not just this conversation.
  agentModel?: string;
  onAgentModelChange: (modelIdentifier: string) => void | Promise<void>;
  // The session's permission mode and its change handler (persists + reflects on the
  // server). Surfaced here as a selector beside agent/model so it's adjustable inline.
  permissionMode?: PermissionMode;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  // Whether this machine confines what tools may touch, and whether it can. Surfaced beside the
  // permission mode because the two answer one question together — what this session is allowed
  // to do — and half of it living three screens into Settings meant the riskier half was the
  // half nobody saw.
  sandboxEnforce?: SandboxEnforce;
  sandboxBackend?: string;
  onSandboxEnforceChange?: (enforce: SandboxEnforce) => void;
  // Running token totals for the session, summed from the model's reported usage.
  // Null until the first turn reports usage.
  tokenUsage?: TokenUsage | null;
  // Compact the conversation now (summarize the older history). Shown once a
  // session has real context to compact.
  onCompact?: () => void;
  // True while a compaction pass is running, so the Compact control reflects the
  // in-progress state (spinner + disabled) rather than inviting another click.
  isCompacting?: boolean;
  // How many of the most recent user turns are kept verbatim during compaction
  // (the server's compaction.keep_recent_turns). The manual compact button is
  // available once there are more user messages than this threshold.
  compactionKeepRecentTurns: number;
  // How many user messages exist in the current session. Used together with
  // compactionKeepRecentTurns to decide whether compaction would be meaningful.
  compactionUserCount: number;
}

// A filling circle for how full the model's context window is. The arc grows with
// the fill fraction and shifts colour as it approaches the limit (blue -> amber ->
// red) so a nearly-full context reads at a glance. Sized 13px to match the icons.
function ContextFillRing({ fraction }: { fraction: number }) {
  const clamped = Math.max(0, Math.min(1, fraction));
  const radius = 5.5;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - clamped);
  const stroke = clamped >= 0.9
    ? "var(--chakra-colors-red-solid)"
    : clamped >= 0.75
      ? "var(--chakra-colors-orange-solid)"
      : "var(--chakra-colors-blue-solid)";
  return (
    <Box w="13px" h="13px" flexShrink={0} display="flex" alignItems="center" justifyContent="center">
      <svg width="13" height="13" viewBox="0 0 14 14">
        <circle cx="7" cy="7" r={radius} fill="none" stroke="var(--chakra-colors-bg-muted)" strokeWidth="2" />
        <circle
          cx="7"
          cy="7"
          r={radius}
          fill="none"
          stroke={stroke}
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 7 7)"
        />
      </svg>
    </Box>
  );
}

// The ChatGPT/Codex plan usage for the token view, but only when the active model is
// on the chatgpt provider. Refreshed on each turn's falling edge (streaming
// true -> false): the server re-snapshots the plan limits from that turn's response
// headers, so the meters are current right after each send/receive.
function useChatGPTUsage(agentModel: string | undefined, isStreaming: boolean): ChatGPTUsage | null {
  const isChatGPT = !!agentModel && agentModel.startsWith("chatgpt/");
  const [usage, setUsage] = useState<ChatGPTUsage | null>(null);

  // Fetch whenever the model is on the chatgpt provider and no turn is streaming.
  // That single condition covers both mount and the falling edge of each turn
  // (streaming true -> false) — which is exactly when the server has re-snapshotted
  // the plan limits from that turn's response headers.
  useEffect(() => {
    if (!isChatGPT || isStreaming) return;
    let cancelled = false;
    fetchChatGPTAuthStatus()
      .then((status) => {
        if (!cancelled) setUsage(status?.usage ?? null);
      })
      .catch((caught) => swallowed({ component: "chat-input", operation: "read the ChatGPT plan usage" }, caught));
    return () => {
      cancelled = true;
    };
  }, [isChatGPT, isStreaming]);

  return isChatGPT ? usage : null;
}

// The context-usage chip: a fill ring + percent, then the current context size
// against the model's window. It reflects the latest exchange actually occupying
// the window (not the cumulative session sum, which lives in the tooltip). On the
// chatgpt provider the tooltip also carries the account's plan usage (5h + weekly).
function ContextUsageChip({
  tokenUsage,
  chatgptUsage,
}: {
  tokenUsage?: TokenUsage | null;
  chatgptUsage?: ChatGPTUsage | null;
}) {
  const translation = useTranslations("ChatInput");
  if (!tokenUsage || tokenUsage.contextTokens <= 0) return null;
  const hasContext = tokenUsage.contextWindow > 0;
  const contextFraction = hasContext ? tokenUsage.contextTokens / tokenUsage.contextWindow : 0;
  const contextPercent = Math.min(100, Math.round(contextFraction * 100));
  const tooltipContent = (
    <Box whiteSpace="nowrap">
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("sessionTotals")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("input")}><Text>{tokenUsage.inputTokens.toLocaleString()}</Text></InlineField>
        <InlineField label={translation("output")}><Text>{tokenUsage.outputTokens.toLocaleString()}</Text></InlineField>
        <InlineField label={translation("total")}><Text>{tokenUsage.totalTokens.toLocaleString()}</Text></InlineField>
        {tokenUsage.cacheReadTokens > 0 && (
          <InlineField label={translation("cacheReads")}><Text>{tokenUsage.cacheReadTokens.toLocaleString()}</Text></InlineField>
        )}
        {tokenUsage.reasoningTokens > 0 && (
          <InlineField label={translation("reasoning")}><Text>{tokenUsage.reasoningTokens.toLocaleString()}</Text></InlineField>
        )}
        <InlineField label={translation("modelCalls")}><Text>{tokenUsage.modelCalls}</Text></InlineField>
      </Flex>
      <Separator my={2} />
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("usageThisTurn")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("input")}><Text>{tokenUsage.contextInputTokens.toLocaleString()}</Text></InlineField>
        <InlineField label={translation("output")}><Text>{tokenUsage.contextOutputTokens.toLocaleString()}</Text></InlineField>
        {hasContext && (
          <InlineField label={translation("window")}><Text>{tokenUsage.contextWindow.toLocaleString()}</Text></InlineField>
        )}
      </Flex>
      {chatgptUsage && chatgptUsage.windows.length > 0 && (
        <>
          <Separator my={2} />
          <Box w="210px" whiteSpace="normal">
            <ChatGPTUsageMeters usage={chatgptUsage} />
          </Box>
        </>
      )}
    </Box>
  );
  return (
    <Tooltip
      content={tooltipContent}
      rich
      openDelay={200}
      closeDelay={60}
      positioning={{ placement: "top" }}
    >
      <Flex
        align="center"
        gap={1.5}
        h={8}
        px={2}
        borderRadius="md"
        border="1px solid"
        borderColor="border"
        bg="bg"
        color="fg.subtle"
        flexShrink={0}
      >
        {hasContext && (
          <>
            <ContextFillRing fraction={contextFraction} />
            <Text data-composer-context-percent="" textStyle="fieldLabel" whiteSpace="nowrap">
              {contextPercent}%
            </Text>
            <Separator data-composer-context-detail="" orientation="vertical" h={3.5} flexShrink={0} />
          </>
        )}
        <Box data-composer-context-detail="" display="flex" alignItems="center" flexShrink={0}>
          <LuCoins size={13} />
        </Box>
        <Text data-composer-context-detail="" textStyle="fieldLabel" whiteSpace="nowrap">
          {tokenUsage.contextTokens.toLocaleString()}
          {hasContext ? ` / ${tokenUsage.contextWindow.toLocaleString()}` : ""}
        </Text>
      </Flex>
    </Tooltip>
  );
}

export function ChatInput({
  onSend,
  onAbort,
  isStreaming,
  disabled,
  sessionId,
  initialDraft = "",
  onDraftChange,
  workingDirectory,
  directoryValid = false,
  agents,
  selectedAgent,
  onAgentChange,
  models,
  modelProviders,
  recentModels = [],
  agentModel = "",
  onAgentModelChange,
  permissionMode = "default",
  onPermissionModeChange,
  sandboxEnforce = "required",
  sandboxBackend = "",
  onSandboxEnforceChange,
  tokenUsage,
  onCompact,
  isCompacting = false,
  compactionKeepRecentTurns,
  compactionUserCount,
}: ChatInputProps) {
  const translation = useTranslations("ChatInput");
  const chatgptUsage = useChatGPTUsage(agentModel, isStreaming);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dropZoneRef = useRef<HTMLDivElement>(null);
  const [inputValue, setInputValue] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploadingCount, setUploadingCount] = useState(0);
  const [dragActive, setDragActive] = useState(false);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [messageHistory, setMessageHistory] = useState<string[]>([]);
  const draftInputRef = useRef("");
  const persistedDraftKeyRef = useRef("");
  const latestInputValueRef = useRef("");
  const [sendPending, setSendPending] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const [compactConfirmOpen, setCompactConfirmOpen] = useState(false);
  // Dictation, which is off until somebody turns it on in Settings — so the microphone is
  // absent rather than disabled when they have not. `recording` holds the take in progress:
  // the button is a toggle, and what a toggle owns is the thing it can stop.
  const [dictationEnabled, setDictationEnabled] = useState(false);
  const [dictationState, setDictationState] = useState<DictationState>("idle");
  const [recording, setRecording] = useState<Dictation | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  // The composer's file-attach affordance is gated on the agent model's
  // capabilities (models.dev): a text-only model cannot process attachments, so
  // offering to attach is misleading. Unknown/custom models are not blocked.
  const effectiveModelId = agentModel;
  const visionSupported = modelSupportsVision(models, effectiveModelId);
  const attachmentTooltipContent = (
    <Box w="420px" maxW="calc(100vw - 32px)">
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("fileAttachments")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("images")}>
          <Text>{visionSupported ? translation("imagesSupported") : translation("imagesUnsupported")}</Text>
        </InlineField>
      </Flex>
    </Box>
  );

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const draftKey = sessionId || "__new__";

  useEffect(() => {
    latestInputValueRef.current = inputValue;
  }, [inputValue]);

  // Seed the composer from the session's stored draft. The draft is fetched from its own
  // endpoint now (the session registry no longer carries it), so it can land after this
  // composer has already mounted for the session — hence the second condition, which
  // accepts a late arrival only while the box is still untouched and never overwrites
  // what the user has begun typing.
  useEffect(() => {
    const isNewSession = persistedDraftKeyRef.current !== draftKey;
    if (!isNewSession && !(initialDraft && latestInputValueRef.current === "")) return;
    persistedDraftKeyRef.current = draftKey;
    const restoredDraft = sessionId ? initialDraft : "";
    setInputValue(restoredDraft);
    draftInputRef.current = restoredDraft;
    setHistoryIndex(-1);
  }, [draftKey, initialDraft, sessionId]);

  useEffect(() => {
    if (!sessionId || !onDraftChange) return;
    const timer = window.setTimeout(() => {
      onDraftChange(latestInputValueRef.current);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [inputValue, onDraftChange, sessionId]);

  useEffect(() => {
    if (!sessionId || !onDraftChange) return;
    return () => {
      onDraftChange(latestInputValueRef.current);
    };
  }, [onDraftChange, sessionId]);

  // Fetch message history when the working directory changes.
  useEffect(() => {
    let cancelled = false;
    if (!workingDirectory) {
      queueMicrotask(() => {
        if (!cancelled) setMessageHistory([]);
      });
      return () => {
        cancelled = true;
      };
    }
    fetchMessageHistory(workingDirectory)
      .then((history) => {
        if (!cancelled) setMessageHistory(history);
      })
      .catch((caught) => swallowed({ component: "chat-input", operation: "read the message history" }, caught));
    return () => {
      cancelled = true;
    };
  }, [workingDirectory]);

  // Web path: the browser only ever hands us file *bytes*, so a sandboxed build (or a
  // remote-server connection, where a local path is meaningless) uploads them.
  async function handleFiles(files: FileList | File[]) {
    const selected = Array.from(files);
    if (selected.length === 0) return;
    setUploadingCount((current) => current + selected.length);
    for (const file of selected) {
      try {
        const uploaded = await uploadFile(file);
        setAttachments((current) => [...current, uploaded]);
      } catch {
        // The send button stays disabled only while uploads are in flight; a failed
        // upload simply does not become an attachment.
      } finally {
        setUploadingCount((current) => Math.max(0, current - 1));
      }
    }
  }

  // Desktop path: the file is referenced by its real OS path, in place — no copy. Only
  // valid when the server is this machine; otherwise the byte path above is used.
  async function attachByPaths(paths: string[]) {
    if (paths.length === 0) return;
    setUploadingCount((current) => current + paths.length);
    for (const path of paths) {
      try {
        const attachment = await referenceAttachment(path);
        setAttachments((current) => [...current, attachment]);
      } catch {
        // A path that no longer exists (or a race with a rename) simply does not attach.
      } finally {
        setUploadingCount((current) => Math.max(0, current - 1));
      }
    }
  }

  // Whether the microphone is offered, and what the model behind it is doing. Asking with
  // `prepare` also starts the load, so the weights come up while the composer is simply on
  // screen — by the time anybody presses the button it is usually already warm. Polled only
  // while it is actually loading: there is nothing to watch once it is ready, and a poll that
  // outlives its question is a poll nobody remembers adding.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const read = (prepare: boolean) => {
      fetchDictationStatus(prepare)
        .then((status) => {
          if (cancelled) return;
          setDictationEnabled(status.enabled);
          setDictationState(status.state);
          if (status.enabled && status.state === "loading") {
            timer = window.setTimeout(() => read(false), 1000);
          }
        })
        .catch((caught) => swallowed({ component: "chat-input", operation: "read the dictation status" }, caught));
    };
    read(true);
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "settings_changed") read(true);
    });
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      unsubscribe();
    };
  }, []);

  // Stop the microphone if the composer goes away mid-recording. Without it the tracks stay
  // open and the browser keeps showing the machine as listening, which is the one bug in this
  // area a person would rightly find alarming.
  const recordingRef = useRef<Dictation | null>(null);
  useEffect(() => {
    recordingRef.current = recording;
  }, [recording]);
  useEffect(() => () => recordingRef.current?.cancel(), []);

  // The dictation toggle. Press once to start, again to stop: on stop the samples go to the
  // daemon, which transcribes them on this machine, and the text is *appended to what is
  // already typed* rather than replacing it — dictation is another way to write into the box,
  // not a separate box.
  async function handleDictationClick() {
    if (transcribing) return;
    const active = recording;
    if (active) {
      setRecording(null);
      setTranscribing(true);
      try {
        const spoken = (await active.stop()).trim();
        if (!spoken) return;
        setInputValue((current) => {
          const next = current.trim() ? `${current.trimEnd()} ${spoken}` : spoken;
          latestInputValueRef.current = next;
          return next;
        });
        inputRef.current?.focus();
      } catch (caught) {
        toaster.create({
          type: "error",
          title: translation("dictationFailed"),
          description: dictationReason(caught),
          closable: true,
        });
      } finally {
        setTranscribing(false);
      }
      return;
    }
    try {
      setRecording(await startDictation());
    } catch (caught) {
      toaster.create({
        type: "error",
        title: translation("dictationFailed"),
        description: dictationReason(caught),
        closable: true,
      });
    }
  }

  // The attach button: on the desktop app talking to a local server, open the native
  // picker and reference the chosen files by path; otherwise fall back to the web
  // <input>, which yields bytes to upload.
  /**
   * What to tell somebody about a dictation that did not happen.
   *
   * `DictationRecordingError` carries a catalogue key because the module that raises it has no
   * hook to translate with. Anything else — a transcription the daemon refused, a network that
   * went — already arrives as a sentence in the person's language, and is passed through.
   */
  function dictationReason(caught: unknown): string {
    if (caught instanceof DictationRecordingError) {
      return translation(caught.message as Parameters<typeof translation>[0], caught.values);
    }
    return errorMessage(caught);
  }

  async function handleAttachClick() {
    if (isTauri()) {
      const paths = await pickDesktopFilePaths();
      await attachByPaths(paths);
      return;
    }
    fileInputRef.current?.click();
  }

  // Native (Tauri) file drops never reach the HTML drag events — they arrive on the
  // webview's own drop stream with real paths. We keep the latest drop action in a ref
  // so the listener can register once yet always call the current closure. A drop only
  // becomes an in-place attachment when it lands over the composer *and* the server is
  // local; over a remote server a local path is meaningless, so the drop is ignored
  // (the attach button still works, falling back to a byte upload).
  const desktopDropRef = useRef<(paths: string[]) => void>(() => {});
  useEffect(() => {
    desktopDropRef.current = (paths: string[]) => {
      void (async () => {
        await attachByPaths(paths);
      })();
    };
  });

  useEffect(() => {
    if (!isTauri()) return;
    let cancelled = false;
    let unlisten: (() => void) | undefined;
    // A file dropped ANYWHERE on the window attaches to this composer — matching the
    // desktop convention (drop a file on the chat = attach it). An earlier version
    // only accepted drops landing exactly on the small composer box, so most drops
    // silently did nothing; the composer border still lights up (dragActive) as the cue.
    void watchDesktopFileDrop((event) => {
      if (event.phase === "leave") {
        setDragActive(false);
        return;
      }
      if (event.phase === "drop") {
        setDragActive(false);
        desktopDropRef.current(event.paths);
        return;
      }
      // enter / over: a file is hovering the window — cue the drop affordance.
      setDragActive(true);
    }).then((fn) => {
      if (cancelled) fn();
      else unlisten = fn;
    });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  function removeAttachment(uploadId: string) {
    setAttachments((current) => current.filter((attachment) => attachment.upload_id !== uploadId));
  }

  async function handleSubmit() {
    const trimmed = inputValue.trim();
    // A typed message is always required — an attachment is context on top of what you
    // say, never a substitute for it. No auto-injected placeholder text.
    if (!trimmed) return;
    if (!directoryValid) return;
    if (uploadingCount > 0) return;
    const startedAt = performance.now();
    setSendPending(true);
    const sendText = trimmed;
    const dataParts = attachments.length > 0 ? [{ kind: "attachments", attachments }] : [];
    try {
      // While the agent is busy this enqueues for the next turn (handled upstream).
      await onSend(sendText, dataParts);
      setHistoryIndex(-1);
      setInputValue("");
      latestInputValueRef.current = "";
      onDraftChange?.("");
      setAttachments([]);
      // Persist to backend and prepend to local list for immediate recall.
      if (trimmed) {
        setMessageHistory((previous) => [trimmed, ...previous]);
        if (workingDirectory) {
          saveMessageHistory(workingDirectory, trimmed)
            .catch((caught) => swallowed({ component: "chat-input", operation: "save the message history" }, caught));
        }
      }
    } finally {
      window.setTimeout(() => setSendPending(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }

  async function handleAbortClick() {
    const startedAt = performance.now();
    setStopPending(true);
    try {
      await onAbort();
    } finally {
      window.setTimeout(() => setStopPending(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
      return;
    }
    if (event.key === "ArrowUp" && messageHistory.length > 0 && inputRef.current?.selectionStart === 0) {
      event.preventDefault();
      // Save the current draft when first navigating up, so it can be
      // restored when the user navigates back down past all history items.
      if (historyIndex === -1) {
        draftInputRef.current = inputValue;
      }
      const nextIndex = historyIndex === -1
        ? 0
        : Math.min(messageHistory.length - 1, historyIndex + 1);
      setHistoryIndex(nextIndex);
      setInputValue(messageHistory[nextIndex]);
      return;
    }
    if (event.key === "ArrowDown" && inputRef.current?.selectionStart === inputValue.length) {
      const nextIndex = historyIndex <= 0 ? -1 : historyIndex - 1;
      setHistoryIndex(nextIndex);
      // Restore the saved draft when navigating back to the "no history" position.
      setInputValue(nextIndex === -1 ? draftInputRef.current : messageHistory[nextIndex]);
      event.preventDefault();
      return;
    }
  }

  return (
    <Box
      position="relative"
      pb={2}
      containerType="inline-size"
      // The selectors row is `nowrap`, so every control in it has to be told how to give way as
      // the composer narrows. A control that is in none of these rules keeps its full width for
      // ever, and because nothing in the row can shrink past its content the row simply grows
      // wider than the box — which is seen as the context chip riding over the selectors rather
      // than as an overflow. The sandbox toggle was added without an entry here and did exactly
      // that; it now sheds its label with the other toggles and can shrink like its neighbours.
      //
      // **Drop before you truncate.** That ordering is the whole design and it was the wrong way
      // round: the first band capped every control's width while the first band that removed any
      // *label* was three steps further down, so a composer around 800px wide showed a row of
      // words cut mid-syllable — "ChatGPT Su… > GP…" for the model, "Global ac…" for the sandbox.
      // A hidden label leaves an icon and a colour, which still say which control this is. A
      // truncated one says nothing and takes the same room to say it.
      //
      // So each band now removes the least load-bearing *text* remaining, and the caps exist only
      // to stop a pathologically long name from bullying its neighbours — not as the mechanism.
      // `data-composer-*` marks what may be dropped; a control with no marker is one whose text
      // is the whole point.
      css={{
        // First to go, and by a distance: the provider before the model, and the capability
        // glyphs beside it. Nobody navigates by the provider — it is implied by the model, and it
        // is what pushes that chip into truncating the one name that matters.
        "@container (max-width: 900px)": {
          "& [data-composer-model-provider], & [data-composer-model-capabilities]": { display: "none" },
          "& [data-composer-agent-control]": { minWidth: "0 !important", maxWidth: "190px", flexShrink: 1 },
          "& [data-composer-model]": { minWidth: "0 !important", maxWidth: "230px", flexShrink: 1 },
          "& [data-composer-permission-control]": { minWidth: "0 !important", maxWidth: "180px", flexShrink: 1 },
          "& [data-composer-sandbox-control]": { minWidth: "0 !important", maxWidth: "170px", flexShrink: 1 },
        },
        // Then the sandbox word. Of the three toggles it is the one whose icon carries most on its
        // own — a globe against a box, in red against green — so it can lose its label a step
        // before the others without the row looking half-collapsed.
        "@container (max-width: 820px)": {
          "& [data-composer-sandbox-label]": { display: "none" },
        },
        // Then the permission word and the exact token counts. The percentage stays: it is the
        // part of the usage chip anybody actually reads at a glance.
        "@container (max-width: 720px)": {
          "& [data-composer-permission-label], & [data-composer-compact-label]": { display: "none" },
          "& [data-composer-context-detail]": { display: "none" },
        },
        // Last, the two identity labels. These are the ones worth keeping longest, which is why
        // they go last rather than being capped into ellipses early.
        "@container (max-width: 560px)": {
          "& [data-composer-agent-label], & [data-composer-model-label], & [data-composer-context-percent]": { display: "none" },
          // Said here as well as on the elements themselves. The `flexWrap` props are one edit
          // away from being changed by somebody adding a control, and an easy one to miss; this
          // is where the rest of the narrow-screen behaviour is stated, so the rule that matters
          // most at a narrow width belongs here too.
          "& [data-composer-row], & [data-composer-selectors]": { flexWrap: "nowrap" },
        },
        // Never two rows.
        //
        // This used to wrap here, and the reasoning is still in the history: with labels showing,
        // the row genuinely could not hold every control and the usage chip. But by this width the
        // rules above have already reduced every one of them to its glyph, and four glyphs plus a
        // ring come to about half of a 390pt panel — so the wrap was firing on a width it had
        // stopped applying to, and spending a whole line of the transcript to put one small chip
        // on its own beneath four others.
        //
        // The spacing stays the desktop's. Tightening it here was the obvious next lever and the
        // wrong one: the controls already fit, so the gap was being spent to buy room nothing
        // needed, and a row of glyphs four points apart reads as one crowded control rather than
        // as four. If something is ever added that genuinely does not fit, reduce it the way the
        // rules above reduce everything else — a second line under a text box reads as a layout
        // that has come apart, not as one adapting.

      }}
    >
      <ConfirmDialog
        open={compactConfirmOpen}
        onOpenChange={setCompactConfirmOpen}
        title={translation("compactTitle")}
        confirmLabel={translation("compactConfirm")}
        confirmIcon={<LuFoldVertical size={14} />}
        onConfirm={() => onCompact?.()}
      >
        {translation.rich("compactBody", {
          count: compactionKeepRecentTurns,
          b: (chunks) => <Strong>{chunks}</Strong>,
        })}
      </ConfirmDialog>

      {/* Message input */}
      <Box px={0} mt={2} pb={1.5}>
        {/* Pending attachments sit ABOVE the composer box, not inside it, so the enlarged
            media cards have room and the input stays uncluttered. */}
        {attachments.length > 0 || uploadingCount > 0 ? (
          <Flex gap={2} pb={2} flexWrap="wrap">
            {attachments.map((attachment) => (
              <AttachmentChip
                key={attachment.upload_id}
                attachment={{
                  filename: attachment.filename,
                  path: attachment.path,
                  mimeType: attachment.mime_type,
                  size: attachment.size,
                }}
                onRemove={() => removeAttachment(attachment.upload_id)}
              />
            ))}
            {uploadingCount > 0 ? (
              <Flex align="center" gap={1.5} px={1.5} py={1} border="1px solid" borderColor="border" borderRadius="md" bg="bg.subtle">
                <Box color="blue.fg"><LuPaperclip size={14} /></Box>
                <Text fontSize="xs" color="fg.subtle">{translation("uploading", { count: uploadingCount })}</Text>
              </Flex>
            ) : null}
          </Flex>
        ) : null}
        {/*
          `flex-end`, not `stretch`, and the difference is where the composer's text sits.

          Stretched, the text box takes the height of whatever is tallest in this row — the send
          and attach buttons beside it. A `textarea` lays its text along the top of its content
          box and has no way to centre it, so every pixel by which those buttons out-measure one
          line of text became empty space *under* the text, and the text read as sitting high.
          Which engine you looked in decided whether you saw it: buttons do not come out to the
          same intrinsic height in WebKit as in Blink, and neither does a line box.

          Aligned to the bottom instead, the text box is exactly as tall as what it holds, so a
          single line fills it and is centred by construction. The buttons sit on its bottom edge,
          which is also where they belong as it grows.
        */}
        <Flex align="flex-end" gap={2}>
          <Box
            ref={dropZoneRef}
            display="flex"
            flex={1}
            minW={0}
            onDragEnter={(event) => {
              event.preventDefault();
              setDragActive(true);
            }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={(event) => {
              event.preventDefault();
              setDragActive(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragActive(false);
              void handleFiles(event.dataTransfer.files);
            }}
          >
            <Textarea
              ref={inputRef}
              size="sm"
              variant="outline"
              placeholder={
                disabled
                  ? translation("placeholderConnecting")
                  : !directoryValid
                    ? translation("placeholderInvalidPath")
                    : attachments.length > 0
                      ? translation("placeholderAttachments")
                      : isStreaming
                        ? translation("placeholderStreaming")
                        : translation("placeholderDefault")
              }
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={disabled}
              rows={1}
              fieldSizing="content"
              maxH="44"
              overflowY="auto"
              borderColor={dragActive ? "blue.muted" : directoryValid ? "border" : "red.muted"}
              bg="bg.panel"
              resize="none"
            />
          </Box>
          <Flex align="flex-end" gap={1.5} flexShrink={0}>
            <Input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={(event) => {
                if (event.target.files) void handleFiles(event.target.files);
                event.target.value = "";
              }}
            />
            {dictationEnabled && (
              <Tooltip
                content={
                  recording
                    ? translation("dictationStop")
                    : transcribing
                      ? translation("dictationTranscribing")
                      : dictationState === "loading"
                        ? translation("dictationLoading")
                        : translation("dictationStart")
                }
                openDelay={200}
                positioning={{ placement: "top" }}
              >
                <IconButton
                  aria-label={recording ? translation("dictationStop") : translation("dictationStart")}
                  onClick={() => void handleDictationClick()}
                  size="sm"
                  // Recording is a state the machine is in, not a button that happens to be
                  // pressed, so it is coloured rather than merely outlined — there must be no
                  // way to leave a microphone open without noticing.
                  variant={recording ? "solid" : "outline"}
                  colorPalette={recording ? "red" : undefined}
                  bg={recording ? undefined : "bg"}
                  borderColor={recording ? undefined : "border"}
                  // The spinner covers both waits a person can be in: the model coming up, and
                  // the recording being turned into words. Disabled while it loads rather than
                  // hidden — a button that vanishes and reappears is harder to trust than one
                  // that says it is not ready yet.
                  loading={transcribing || dictationState === "loading"}
                  disabled={disabled || !directoryValid || dictationState === "loading"}
                >
                  {recording ? <LuMicOff /> : <LuMic />}
                </IconButton>
              </Tooltip>
            )}
            <Tooltip
              content={attachmentTooltipContent}
              rich
              openDelay={200}
              closeDelay={60}
              positioning={{ placement: "top" }}
            >
              <IconButton
                aria-label={translation("attachFiles")}
                onClick={() => void handleAttachClick()}
                size="sm"
                variant="outline"
                bg="bg"
                borderColor="border"
                disabled={disabled || !directoryValid}
              >
                <LuPaperclip />
              </IconButton>
            </Tooltip>
            {isStreaming ? (
              <Button
                onClick={handleAbortClick}
                size="sm"
                colorPalette="red"
                variant="solid"
                loading={stopPending}
                loadingText={translation("stopping")}
                disabled={stopPending}
              >
                <Box display="flex" alignItems="center" justifyContent="center" flexShrink={0}>
                  <LuSquare />
                </Box>
                {translation("stop")}
              </Button>
            ) : (
              <Button
                onClick={() => void handleSubmit()}
                size="sm"
                colorPalette="blue"
                variant="solid"
                loading={sendPending}
                loadingText={translation("sending")}
                disabled={sendPending || disabled || !directoryValid || uploadingCount > 0 || !inputValue.trim()}
              >
                <Box display="flex" alignItems="center" justifyContent="center" flexShrink={0}>
                  <LuArrowUp />
                </Box>
                {translation("send")}
              </Button>
            )}
          </Flex>
        </Flex>
      </Box>

      {/* Selectors row (below the input): the agent and model selectors, with the
          context-usage chip and Compact action on the right. */}
      <Flex data-composer-row="" justify="space-between" align="center" columnGap={2} flexWrap="nowrap" px={0} pt={1} pb={2}>
        <Flex data-composer-selectors="" align="center" gap={2} flexWrap="nowrap" flex={1} minW={0}>
          <AgentSelectControl
            agents={agents}
            value={selectedAgent}
            onChange={onAgentChange}
            placeholder={translation("agentPlaceholder")}
            responsiveCompact
          />
          <ModelSelect
            models={models}
            providers={modelProviders}
            recent={recentModels}
            value={agentModel}
            onChange={onAgentModelChange}
            fallbackModelId={agentModel}
            compact
            responsiveCompact
          />
          {/* Adjustable at any point in a session's life, not only before it starts: a
              conversation that begins under manual approvals and earns trust should not have
              to be restarted to run under a looser one. */}
          <PermissionModeControl
            value={permissionMode}
            onChange={(mode) => onPermissionModeChange?.(mode)}
            responsiveCompact
          />
          {/* The same control Settings shows, not a second rendering of the same fact: one
              component means the two can never disagree about what "restricted" looks like. */}
          <SandboxToggleControl
            enforce={sandboxEnforce}
            backend={sandboxBackend}
            onChange={onSandboxEnforceChange}
            responsiveCompact
          />
        </Flex>
        <Flex align="center" gap={2} flexShrink={0} justify="flex-end">
          {onCompact && !!sessionId && !!tokenUsage && tokenUsage.contextTokens > 0 && (isCompacting || compactionUserCount > compactionKeepRecentTurns) && (
            <Button
              variant="outline"
              h={8}
              px={2}
              bg="bg"
              borderColor="border"
              flexShrink={0}
              disabled={isStreaming || isCompacting}
              onClick={() => setCompactConfirmOpen(true)}
              title={isCompacting ? translation("compactingTooltip") : translation("compactTooltip")}
            >
              {isCompacting ? <Spinner size="xs" /> : <LuFoldVertical size={13} />}
              <Text data-composer-compact-label="">{isCompacting ? translation("compacting") : translation("compact")}</Text>
            </Button>
          )}
          <ContextUsageChip tokenUsage={tokenUsage} chatgptUsage={chatgptUsage} />
        </Flex>
      </Flex>

    </Box>
  );
}
