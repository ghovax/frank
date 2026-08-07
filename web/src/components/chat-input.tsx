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
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  LuArrowUp,
  LuCoins,
  LuFoldVertical,
  LuMic,
  LuMicOff,
  LuPaperclip,
  LuSquare,
} from "react-icons/lu";
import {
  fetchChatGPTAuthStatus,
  fetchDictationStatus,
  type DictationState,
  fetchMessageHistory,
  referenceAttachment,
  saveMessageHistory,
  subscribeEvents,
  uploadFile,
  type Attachment,
  type ChatGPTUsage,
  type ModelOption,
  type PermissionMode,
  type ProviderOption,
  type SandboxEnforce,
} from "@/lib/api";
import { DictationRecordingError, startDictation, type Dictation } from "@/lib/dictation";
import { toaster } from "./ui/toaster";
import { ChatGPTUsageMeters } from "./chatgpt-usage-meters";
import {
  AgentSelectControl,
  PermissionModeControl,
  SandboxToggleControl,
} from "./session-controls";
import { isTauri } from "@/lib/tauri";
import { pickDesktopFilePaths, watchDesktopFileDrop } from "@/lib/desktop-files";
import { AttachmentChip } from "./attachment-chips";
import { Tooltip } from "./ui/tooltip";
import { ConfirmDialog } from "./ui/confirm-dialog";
import { ModelSelect, modelSupportsVision } from "./model-select";
// SettingsDialog moved to ChatPanel top bar
import type { TokenUsage } from "@/lib/use-chat";
import { InlineField } from "./ui/display";
import { richTags } from "@/lib/i18n/rich-tags";
import { CONTROL_ICON_SIZE } from "./session-controls";
import { swallowed } from "@/lib/swallowed";
import { useFittedRow } from "@/lib/use-fitted-row";
import { errorMessage } from "@/lib/errors";

interface ChatInputProps {
  // Returns the session id when the send created one, which the composer ignores as the caller's business.
  onSend: (text: string, dataParts?: Record<string, unknown>[]) => void | Promise<void | string>;
  onAbort: () => void | Promise<void>;
  isStreaming: boolean;
  // The connection is gone. Nothing can be sent, and saying why is the point.
  disabled?: boolean;
  // A decision prompt is open, which closes the composer for a different reason than a lost network.
  awaitingDecision?: boolean;
  sessionId?: string | null;
  initialDraft?: string;
  onDraftChange?: (draft: string) => void;
  workingDirectory?: string;
  // Whether the working directory is valid, resolved at the workspace level and used here to gate send.
  directoryValid?: boolean;
  agents: { id: string; name: string; title?: string; description?: string }[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
  models: ModelOption[];
  modelProviders: ProviderOption[];
  recentModels?: { id: string; name: string; provider: string }[];
  // The active agent's configured model, driving the chip and the attachment gates for every session running that agent.
  agentModel?: string;
  onAgentModelChange: (modelIdentifier: string) => void | Promise<void>;
  // The session's permission mode and its change handler, surfaced here so it is adjustable inline.
  permissionMode?: PermissionMode;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  // Whether this machine confines what tools may touch, beside the permission mode because they answer one question together.
  sandboxEnforce?: SandboxEnforce;
  sandboxBackend?: string;
  onSandboxEnforceChange?: (enforce: SandboxEnforce) => void;
  // Running token totals for the session, null until the first turn reports usage.
  tokenUsage?: TokenUsage | null;
  // Compact the conversation now, offered whenever there is one.
  onCompact?: () => void;
  // True while a compaction pass is running, so the control shows progress rather than inviting another click.
  isCompacting?: boolean;
  // The share of the window at which the server reclaims on its own; the manual button appears at half of it.
}

// What the selectors row gives up, and in what order, when it cannot hold everything.
const COMPOSER_FIT_ORDER = [
  "model-provider",
  "model-capabilities",
  "context-detail",
  "compact",
  "sandbox",
  "permission",
  "agent",
  "model",
  "context-percent",
] as const;

// A filling ring for how full the context window is, shifting colour as it approaches the limit.
function ContextFillRing({ fraction }: { fraction: number }) {
  const clamped = Math.max(0, Math.min(1, fraction));
  const radius = 5.5;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - clamped);
  const stroke =
    clamped >= 0.9
      ? "var(--chakra-colors-red-solid)"
      : clamped >= 0.75
        ? "var(--chakra-colors-orange-solid)"
        : "var(--chakra-colors-blue-solid)";
  return (
    <Box
      w="13px"
      h="13px"
      flexShrink={0}
      display="flex"
      alignItems="center"
      justifyContent="center"
    >
      <svg width="13" height="13" viewBox="0 0 14 14">
        <circle
          cx="7"
          cy="7"
          r={radius}
          fill="none"
          stroke="var(--chakra-colors-bg-muted)"
          strokeWidth="2"
        />
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

// The plan usage for the token view, fetched only when the active model is on the chatgpt provider.
function useChatGPTUsage(
  agentModel: string | undefined,
  isStreaming: boolean,
): ChatGPTUsage | null {
  const isChatGPT = !!agentModel && agentModel.startsWith("chatgpt/");
  const [usage, setUsage] = useState<ChatGPTUsage | null>(null);

  // Fetch whenever the model is chatgpt and nothing is streaming, which covers both mount and each turn's end.
  useEffect(() => {
    if (!isChatGPT || isStreaming) return;
    let cancelled = false;
    fetchChatGPTAuthStatus()
      .then((status) => {
        if (!cancelled) setUsage(status?.usage ?? null);
      })
      .catch((caught) =>
        swallowed({ component: "chat-input", operation: "read the ChatGPT plan usage" }, caught),
      );
    return () => {
      cancelled = true;
    };
  }, [isChatGPT, isStreaming]);

  return isChatGPT ? usage : null;
}

// The context-usage chip: a fill ring and percent, then the current context size against the model's window.
function ContextUsageChip({
  tokenUsage,
  chatgptUsage,
  hidden,
}: {
  tokenUsage?: TokenUsage | null;
  chatgptUsage?: ChatGPTUsage | null;
  /** Which of this chip's parts the row has given up; the ring is never among them. */
  hidden: ReadonlySet<string>;
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
        <InlineField label={translation("input")}>
          <Text>{tokenUsage.inputTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("output")}>
          <Text>{tokenUsage.outputTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("total")}>
          <Text>{tokenUsage.totalTokens.toLocaleString()}</Text>
        </InlineField>
        {/* Always shown, because zero cache reads is the reading worth having, and the share is of what was reachable. */}
        <InlineField label={translation("cacheReads")}>
          <Text>
            {tokenUsage.cacheReadTokens.toLocaleString()}
            {tokenUsage.cacheReachableTokens > 0 &&
              ` / ${tokenUsage.cacheReachableTokens.toLocaleString()} (${Math.round(
                (tokenUsage.cacheReadTokens / tokenUsage.cacheReachableTokens) * 100,
              )}%)`}
          </Text>
        </InlineField>
        {tokenUsage.reasoningTokens > 0 && (
          <InlineField label={translation("reasoning")}>
            <Text>{tokenUsage.reasoningTokens.toLocaleString()}</Text>
          </InlineField>
        )}
        <InlineField label={translation("modelCalls")}>
          <Text>{tokenUsage.modelCalls}</Text>
        </InlineField>
      </Flex>
      <Separator my={2} />
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("usageThisTurn")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("input")}>
          <Text>{tokenUsage.contextInputTokens.toLocaleString()}</Text>
        </InlineField>
        <InlineField label={translation("output")}>
          <Text>{tokenUsage.contextOutputTokens.toLocaleString()}</Text>
        </InlineField>
        {hasContext && (
          <InlineField label={translation("window")}>
            <Text>{tokenUsage.contextWindow.toLocaleString()}</Text>
          </InlineField>
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
        // The house control height rather than a number, so this chip matches the buttons beside it on a coarse pointer.
        h="var(--control-height)"
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
            <Text
              data-fit-label="context-percent"
              data-fit-hidden={hidden.has("context-percent") ? "" : undefined}
              textStyle="fieldLabel"
              whiteSpace="nowrap"
            >
              {contextPercent}%
            </Text>
            {hidden.has("context-detail") ? null : (
              <Separator orientation="vertical" h={3.5} flexShrink={0} />
            )}
          </>
        )}
        <Box
          data-fit-label="context-detail"
          data-fit-hidden={hidden.has("context-detail") ? "" : undefined}
          display="flex"
          alignItems="center"
          flexShrink={0}
        >
          <LuCoins size={CONTROL_ICON_SIZE} />
        </Box>
        <Text
          data-fit-label="context-detail"
          data-fit-hidden={hidden.has("context-detail") ? "" : undefined}
          textStyle="fieldLabel"
          whiteSpace="nowrap"
        >
          {tokenUsage.contextTokens.toLocaleString()}
          {hasContext ? ` / ${tokenUsage.contextWindow.toLocaleString()}` : ""}
        </Text>
      </Flex>
    </Tooltip>
  );
}

// Lucide glyphs fill their 24-unit box by different amounts, so a shared box size draws them at different heights;
// `draw` is the box that makes this one's ink match the icon-only buttons next to it.
function ComposerIcon({ draw, children }: { draw: number; children: ReactNode }) {
  return (
    <Box
      display="flex"
      alignItems="center"
      justifyContent="center"
      flexShrink={0}
      css={{ "& svg": { width: `${draw}px`, height: `${draw}px` } }}
    >
      {children}
    </Box>
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
  awaitingDecision,
  directoryValid = false,
  agents,
  selectedAgent,
  onAgentChange,
  models,
  modelProviders,
  recentModels = [],
  agentModel = "",
  onAgentModelChange,
  permissionMode = "ask",
  onPermissionModeChange,
  sandboxEnforce = "required",
  sandboxBackend = "",
  onSandboxEnforceChange,
  tokenUsage,
  onCompact,
  isCompacting = false,
}: ChatInputProps) {
  const translation = useTranslations("ChatInput");
  const chatgptUsage = useChatGPTUsage(agentModel, isStreaming);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dropZoneRef = useRef<HTMLDivElement>(null);
  // Closed only when there is nothing to talk to; an open decision parks the session rather than removing it.
  const composerClosed = disabled;
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
  const { rowRef: selectorsRowRef, hidden: hiddenLabels } = useFittedRow(COMPOSER_FIT_ORDER);
  // Dictation is absent rather than disabled until it is turned on, and `recording` holds the take a toggle can stop.
  const [dictationEnabled, setDictationEnabled] = useState(false);
  const [dictationState, setDictationState] = useState<DictationState>("idle");
  const [recording, setRecording] = useState<Dictation | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  // The attach affordance is gated on the model's vision capability, though unknown models are not blocked.
  const effectiveModelId = agentModel;
  const visionSupported = modelSupportsVision(models, effectiveModelId);
  const attachmentTooltipContent = (
    <Box w="420px" maxW="calc(100vw - 32px)">
      <Text fontWeight="semibold" mb={1} color="fg">
        {translation("fileAttachments")}
      </Text>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("images")}>
          <Text>
            {visionSupported ? translation("imagesSupported") : translation("imagesUnsupported")}
          </Text>
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

  // Seed the composer from the session's stored draft, accepting one that lands after the composer mounted.
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
      .catch((caught) =>
        swallowed({ component: "chat-input", operation: "read the message history" }, caught),
      );
    return () => {
      cancelled = true;
    };
  }, [workingDirectory]);

  // The browser only ever hands us file bytes, so a sandboxed or remote build uploads them.
  async function handleFiles(files: FileList | File[]) {
    const selected = Array.from(files);
    if (selected.length === 0) return;
    setUploadingCount((current) => current + selected.length);
    for (const file of selected) {
      try {
        const uploaded = await uploadFile(file);
        setAttachments((current) => [...current, uploaded]);
      } catch {
        // Send stays disabled only while uploads are in flight; a failed upload simply never becomes an attachment.
      } finally {
        setUploadingCount((current) => Math.max(0, current - 1));
      }
    }
  }

  // On the desktop the file is referenced by its real path in place, valid only when the server is this machine.
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

  // Whether the microphone is offered and what its model is doing, asked with `prepare` so the weights warm up.
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
        .catch((caught) =>
          swallowed({ component: "chat-input", operation: "read the dictation status" }, caught),
        );
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

  // Stop the microphone if the composer goes away mid-recording, so the machine never keeps listening.
  const recordingRef = useRef<Dictation | null>(null);
  useEffect(() => {
    recordingRef.current = recording;
  }, [recording]);
  useEffect(() => () => recordingRef.current?.cancel(), []);

  // The dictation toggle: press to start and again to stop, appending the text to what is already typed.
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

  // On the desktop open the native picker and reference files by path; otherwise fall back to the web input.
  /** What to tell somebody about a dictation that did not happen, translating the raised catalogue key. */
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

  // Native file drops arrive on the webview's own stream with real paths, so the listener registers once and reads a ref.
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
    // A file dropped anywhere on the window attaches here, matching the desktop convention.
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
    // A typed message is always required: an attachment is context on top of what you say, never a substitute.
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
          saveMessageHistory(workingDirectory, trimmed).catch((caught) =>
            swallowed({ component: "chat-input", operation: "save the message history" }, caught),
          );
        }
      }
    } finally {
      window.setTimeout(
        () => setSendPending(false),
        Math.max(0, 450 - (performance.now() - startedAt)),
      );
    }
  }

  async function handleAbortClick() {
    const startedAt = performance.now();
    setStopPending(true);
    try {
      await onAbort();
    } finally {
      window.setTimeout(
        () => setStopPending(false),
        Math.max(0, 450 - (performance.now() - startedAt)),
      );
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
      return;
    }
    if (
      event.key === "ArrowUp" &&
      messageHistory.length > 0 &&
      inputRef.current?.selectionStart === 0
    ) {
      event.preventDefault();
      // Save the current draft when first navigating up, so it returns when the user comes back down.
      if (historyIndex === -1) {
        draftInputRef.current = inputValue;
      }
      const nextIndex =
        historyIndex === -1 ? 0 : Math.min(messageHistory.length - 1, historyIndex + 1);
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
    <Box position="relative" pb={2}>
      <ConfirmDialog
        open={compactConfirmOpen}
        onOpenChange={setCompactConfirmOpen}
        title={translation("compactTitle")}
        confirmLabel={translation("compactConfirm")}
        confirmIcon={<LuFoldVertical size={14} />}
        onConfirm={() => onCompact?.()}
      >
        {translation.rich("compactBody", richTags)}
      </ConfirmDialog>

      {/* Message input */}
      <Box px={0} mt={2} pb={1.5}>
        {/* Pending attachments sit above the composer box, so the media cards have room and the input stays clear. */}
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
              <Flex
                align="center"
                gap={1.5}
                px={1.5}
                py={1}
                border="1px solid"
                borderColor="border"
                borderRadius="md"
                bg="bg.subtle"
              >
                <Box color="blue.fg">
                  <LuPaperclip size={14} />
                </Box>
                <Text fontSize="xs" color="fg.subtle">
                  {translation("uploading", { count: uploadingCount })}
                </Text>
              </Flex>
            ) : null}
          </Flex>
        ) : null}
        {/* Aligned to the bottom rather than stretched, so a single line of text is centred by construction. */}
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
              // Sized in `globals.css`, where the control height it has to match is defined.
              data-composer-input=""
              size="sm"
              variant="outline"
              placeholder={
                // Ordered by what the person can do about it, so an open decision says what happens to what you type.
                disabled
                  ? translation("placeholderConnecting")
                  : awaitingDecision
                    ? translation("placeholderAwaitingDecision")
                    : !directoryValid
                      ? translation("placeholderInvalidPath")
                      : attachments.length > 0
                        ? translation("placeholderAttachments")
                        : isCompacting
                          ? // Compaction runs as a turn, so this says the message drains when the fold is done rather than next turn.
                            translation("placeholderCompacting")
                          : isStreaming
                            ? translation("placeholderStreaming")
                            : translation("placeholderDefault")
              }
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={composerClosed}
              rows={1}
              fieldSizing="content"
              maxH="44"
              overflowY="auto"
              borderColor={dragActive ? "blue.muted" : directoryValid ? "border" : "red.muted"}
              bg="bg.panel"
              resize="none"
            />
          </Box>
          {/* The same gap as the row of controls below, so the composer has one rhythm rather than two. */}
          <Flex align="flex-end" gap={2} flexShrink={0}>
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
                  aria-label={
                    recording ? translation("dictationStop") : translation("dictationStart")
                  }
                  onClick={() => void handleDictationClick()}
                  size="sm"
                  // Recording is a state the machine is in, so it is coloured rather than outlined and cannot be left open unnoticed.
                  variant={recording ? "solid" : "outline"}
                  colorPalette={recording ? "red" : undefined}
                  bg={recording ? undefined : "bg"}
                  borderColor={recording ? undefined : "border"}
                  // The spinner covers both waits, and the button is disabled while loading rather than hidden.
                  loading={transcribing || dictationState === "loading"}
                  disabled={composerClosed || !directoryValid || dictationState === "loading"}
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
                disabled={composerClosed || !directoryValid}
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
                // Not while the conversation is being folded, since Stop would describe something the model is not doing.
                disabled={stopPending || isCompacting}
                title={isCompacting ? translation("stopUnavailableWhileCompacting") : undefined}
              >
                <ComposerIcon draw={18}>
                  <LuSquare />
                </ComposerIcon>
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
                disabled={
                  sendPending ||
                  composerClosed ||
                  !directoryValid ||
                  uploadingCount > 0 ||
                  !inputValue.trim()
                }
              >
                <ComposerIcon draw={22}>
                  <LuArrowUp />
                </ComposerIcon>
                {translation("send")}
              </Button>
            )}
          </Flex>
        </Flex>
      </Box>

      {/* One line, kept one line by measurement: labels are given up in order until the row fits, with a clipped edge as the guarantee. */}
      <Flex
        ref={selectorsRowRef}
        align="center"
        gap={2}
        flexWrap="nowrap"
        px={0}
        pt={1}
        pb={2}
        overflow="clip"
        // Enough for a focus ring to bleed past the edge and nothing more.
        css={{ overflowClipMargin: "3px" }}
      >
        <AgentSelectControl
          agents={agents}
          value={selectedAgent}
          onChange={onAgentChange}
          placeholder={translation("agentPlaceholder")}
          fitted
          labelHidden={hiddenLabels.has("agent")}
        />
        <ModelSelect
          models={models}
          providers={modelProviders}
          recent={recentModels}
          value={agentModel}
          onChange={onAgentModelChange}
          fallbackModelId={agentModel}
          compact
          fitted
          providerHidden={hiddenLabels.has("model-provider")}
          capabilitiesHidden={hiddenLabels.has("model-capabilities")}
          labelHidden={hiddenLabels.has("model")}
        />
        {/* Adjustable at any point in a session's life, so a conversation need not restart to run under a looser mode. */}
        <PermissionModeControl
          value={permissionMode}
          onChange={(mode) => {
            if (mode) onPermissionModeChange?.(mode);
          }}
          fitted
          labelHidden={hiddenLabels.has("permission")}
        />
        {/* The same control Settings shows, so the two can never disagree about what a mode looks like. */}
        <SandboxToggleControl
          enforce={sandboxEnforce}
          backend={sandboxBackend}
          onChange={onSandboxEnforceChange}
          fitted
          labelHidden={hiddenLabels.has("sandbox")}
        />
        {/* What the turn has spent, pushed to the far end by a margin rather than by a spacer the fit would have to ignore. */}
        <Flex ms="auto" align="center" gap={2} flexShrink={0}>
          {/* Offered whenever there is a conversation to compact: when to do it is the reader's judgement, not a threshold's. */}
          {onCompact && !!sessionId && (
            <Button
              data-fit-control="compact"
              {...(hiddenLabels.has("compact") ? { "data-fit-collapsed": "" } : {})}
              variant="outline"
              h="var(--control-height)"
              // Stated rather than inherited, because the button recipe's own gap is not the one this row uses.
              gap={1.5}
              px={2}
              justifyContent="center"
              bg="bg"
              borderColor="border"
              flexShrink={0}
              disabled={isStreaming || isCompacting}
              onClick={() => setCompactConfirmOpen(true)}
              title={
                isCompacting ? translation("compactingTooltip") : translation("compactTooltip")
              }
            >
              {isCompacting ? (
                <Spinner boxSize={`${CONTROL_ICON_SIZE}px`} borderWidth="1.5px" />
              ) : (
                <LuFoldVertical size={CONTROL_ICON_SIZE} />
              )}
              <Text
                data-fit-label="compact"
                data-fit-hidden={hiddenLabels.has("compact") ? "" : undefined}
              >
                {isCompacting ? translation("compacting") : translation("compact")}
              </Text>
            </Button>
          )}
          <ContextUsageChip
            tokenUsage={tokenUsage}
            chatgptUsage={chatgptUsage}
            hidden={hiddenLabels}
          />
        </Flex>
      </Flex>
    </Box>
  );
}
