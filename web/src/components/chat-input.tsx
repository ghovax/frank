"use client";

import {
  Box,
  Button,
  createListCollection,
  Flex,
  IconButton,
  Input,
  Portal,
  Select,
  Separator,
  Spinner,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";
import { LuArrowUp, LuCoins, LuFoldVertical, LuPaperclip, LuSquare, LuUser } from "react-icons/lu";
import { fetchChatGPTAuthStatus, fetchMessageHistory, referenceAttachment, saveMessageHistory, uploadFile, type Attachment, type ChatGPTUsage, type ModelOption, type PermissionMode, type ProviderOption } from "@/lib/api";
import { ChatGPTUsageMeters } from "./chatgpt-usage-meters";
import { PermissionModeControl } from "./session-controls";
import { activeConnectionIsLocal } from "@/lib/connection";
import { isTauri } from "@/lib/connection-store";
import { pickDesktopFilePaths, watchDesktopFileDrop } from "@/lib/desktop-files";
import { AttachmentChip, ArtifactAnnotationChips } from "./attachment-chips";
import { annotationsPayload, type ArtifactAnnotationRecord, type ArtifactImageAnnotation } from "@/lib/artifact-annotations";
import { Tooltip } from "./ui/tooltip";
import { ConfirmDialog } from "./ui/confirm-dialog";
import { ModelSelect, modelSupportsVision } from "./model-select";
// SettingsDialog moved to ChatPanel top bar
import type { TokenUsage } from "@/lib/use-chat";
import { InlineField } from "./ui/display";
import { Strong } from "./ui/semantic";

interface ChatInputProps {
  onSend: (text: string, dataParts?: Record<string, unknown>[]) => void | Promise<void>;
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
  artifactAnnotationRecords?: ArtifactAnnotationRecord[];
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
      .catch(() => {});
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
      {tokenUsage.agentTotalTokens > 0 && (
        <>
          <Separator my={2} />
          <Text fontWeight="semibold" mb={1} color="fg">
            {translation("agents")}
          </Text>
          <Flex direction="column" ps={2} gap={1}>
            <InlineField label={translation("input")}><Text>{tokenUsage.agentInputTokens.toLocaleString()}</Text></InlineField>
            <InlineField label={translation("output")}><Text>{tokenUsage.agentOutputTokens.toLocaleString()}</Text></InlineField>
            <InlineField label={translation("total")}><Text>{tokenUsage.agentTotalTokens.toLocaleString()}</Text></InlineField>
            <InlineField label={translation("modelCalls")}><Text>{tokenUsage.agentModelCalls}</Text></InlineField>
          </Flex>
        </>
      )}
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
  tokenUsage,
  onCompact,
  isCompacting = false,
  compactionKeepRecentTurns,
  compactionUserCount,
  artifactAnnotationRecords = [],
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
  // Pending image annotations waiting to be sent — a typed message is required to send
  // them, so the composer prompts for one (placeholder + Send tooltip) rather than
  // silently disabling Send.
  const hasPendingAnnotations = artifactAnnotationRecords.some((record) => record.annotations.length > 0);

  const agentCollection = useMemo(
    () => createListCollection({
      items: agents.map((agent) => ({ label: agent.title || agent.name, value: agent.id })),
    }),
    [agents]
  );
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

  useEffect(() => {
    if (persistedDraftKeyRef.current === draftKey) return;
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
      .catch(() => {});
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

  // The attach button: on the desktop app talking to a local server, open the native
  // picker and reference the chosen files by path; otherwise fall back to the web
  // <input>, which yields bytes to upload.
  async function handleAttachClick() {
    if (isTauri() && (await activeConnectionIsLocal())) {
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
        if (await activeConnectionIsLocal()) await attachByPaths(paths);
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

  // Annotations drawn on a pending image ride along on the attachment itself, so they
  // travel in the outgoing `attachments` data part when the message is sent.
  function updateAttachmentAnnotations(uploadId: string, annotations: ArtifactImageAnnotation[]) {
    setAttachments((current) =>
      current.map((attachment) =>
        attachment.upload_id === uploadId
          ? { ...attachment, annotations: annotations.length > 0 ? annotations : undefined }
          : attachment,
      ),
    );
  }

  async function handleSubmit() {
    const trimmed = inputValue.trim();
    const activeArtifactAnnotationRecords = artifactAnnotationRecords.filter((record) => record.annotations.length > 0);
    // A typed message is always required — attachments and annotations are context on
    // top of what you say, never a substitute for it. No auto-injected placeholder text.
    if (!trimmed) return;
    if (!directoryValid) return;
    if (uploadingCount > 0) return;
    const startedAt = performance.now();
    setSendPending(true);
    const sendText = trimmed;
    const dataParts = [
      ...(attachments.length > 0 ? [{ kind: "attachments", attachments }] : []),
      ...activeArtifactAnnotationRecords.map((record) => annotationsPayload(record)),
    ];
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
          saveMessageHistory(workingDirectory, trimmed).catch(() => {});
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
      css={{
        "@container (max-width: 900px)": {
          "& [data-composer-agent-control]": { minWidth: "0 !important", maxWidth: "160px", flexShrink: 1 },
          "& [data-composer-model]": { minWidth: "0 !important", maxWidth: "220px", flexShrink: 1 },
          "& [data-composer-permission-control]": { minWidth: "0 !important", maxWidth: "160px", flexShrink: 1 },
        },
        "@container (max-width: 760px)": {
          "& [data-composer-context-detail]": { display: "none" },
          "& [data-composer-model-provider], & [data-composer-model-capabilities]": { display: "none" },
        },
        "@container (max-width: 620px)": {
          "& [data-composer-permission-label], & [data-composer-compact-label]": { display: "none" },
        },
        "@container (max-width: 500px)": {
          "& [data-composer-agent-label], & [data-composer-model-label], & [data-composer-model-capabilities], & [data-composer-context-percent]": { display: "none" },
        },
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
        {/* Pending attachments/annotations sit ABOVE the composer box, not inside it, so
            the enlarged media cards have room and the input stays uncluttered. */}
        {attachments.length > 0 || artifactAnnotationRecords.length > 0 || uploadingCount > 0 ? (
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
                annotations={attachment.annotations}
                onAnnotationsChange={(annotations) => updateAttachmentAnnotations(attachment.upload_id, annotations)}
                onRemove={() => removeAttachment(attachment.upload_id)}
              />
            ))}
            <ArtifactAnnotationChips records={artifactAnnotationRecords} />
            {uploadingCount > 0 ? (
              <Flex align="center" gap={1.5} px={1.5} py={1} border="1px solid" borderColor="border" borderRadius="md" bg="bg.subtle">
                <Box color="blue.fg"><LuPaperclip size={14} /></Box>
                <Text fontSize="xs" color="fg.subtle">{translation("uploading", { count: uploadingCount })}</Text>
              </Flex>
            ) : null}
          </Flex>
        ) : null}
        <Flex align="stretch" gap={2}>
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
                    : hasPendingAnnotations
                      ? translation("placeholderAnnotations")
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
      <Flex justify="space-between" align="center" columnGap={2} flexWrap="nowrap" px={0} pt={1} pb={2}>
        <Flex align="center" gap={2} flexWrap="nowrap" flex={1} minW={0}>
          <Select.Root
            data-composer-agent-control=""
            collection={agentCollection}
            value={[selectedAgent]}
            onValueChange={(details) => {
              if (details.value[0]) onAgentChange(details.value[0]);
            }}
            size="xs"
            w="max-content"
            minW="max-content"
            maxW="none"
            flexShrink={0}
          >
            <Select.Control data-composer-agent-control="" w="max-content" minW="max-content" maxW="none">
              <Select.Trigger
                data-composer-agent-control=""
                w="max-content"
                gap={1.5}
                px={2}
                pe={7}
                bg="bg"
                border="1px solid"
                borderColor="border"
                minW="max-content"
                maxW="none"
                whiteSpace="nowrap"
                fontWeight="medium"
              >
                <Box display="flex" alignItems="center" color="fg.muted" flexShrink={0}>
                  <LuUser size={13} />
                </Box>
                <Select.ValueText data-composer-agent-label="" placeholder={translation("agentPlaceholder")} maxW="none" overflow="visible" textOverflow="clip" whiteSpace="nowrap" />
              </Select.Trigger>
              <Select.IndicatorGroup>
                <Select.Indicator />
              </Select.IndicatorGroup>
            </Select.Control>
            <Portal>
              <Select.Positioner>
                <Select.Content minW="220px" maxW="320px">
                  {agentCollection.items.map((item) => {
                    // Look the description up from the source list by id — the collection item
                    // only reliably carries label/value, so extra fields are read from `agents`.
                    const description = agents.find((agent) => agent.id === item.value)?.description;
                    return (
                      <Select.Item item={item} key={item.value}>
                        <Flex direction="column" minW={0} flex={1}>
                          <Text fontSize="xs" fontWeight="medium" lineHeight="1.2" whiteSpace="nowrap">{item.label}</Text>
                          {description ? (
                            <Text fontSize="2xs" color="fg.muted" lineHeight="1.35" truncate>{description}</Text>
                          ) : null}
                        </Flex>
                        <Select.ItemIndicator />
                      </Select.Item>
                    );
                  })}
                </Select.Content>
              </Select.Positioner>
            </Portal>
          </Select.Root>
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
          <PermissionModeControl value={permissionMode} onChange={(mode) => onPermissionModeChange?.(mode)} responsiveCompact />
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
