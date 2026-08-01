/**
 * Driving one session's turn, and watching it.
 *
 * The shape is the desktop's: all mutation lands on a mutable ref, and React state is refreshed
 * from it at most once a frame. That is not a micro-optimisation. A turn's prose arrives as one
 * part per token, and a `setState` per part means a re-render per token — which a laptop
 * survives and a phone does not.
 *
 * Two other decisions are carried over because they are not obvious and both were paid for:
 *
 *   - **Attach before send.** The worker emits the moment it accepts a message, so a client that
 *     sends first and subscribes second loses the opening of its own turn.
 *   - **`turn` is not `done`.** A session goes idle many times over its life; `done` is the
 *     session itself ending. Conflating them either stops the transcript after one turn or waits
 *     for a process that is not going to die.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  attachSession, cancelTurn, compactSession, fetchSessionTurnsPage, messageParts,
  resolvePermission as resolvePermissionCall, resolveQuestion as resolveQuestionCall,
  sessionCreate, sessionSend,
  type PermissionMode, type SessionStreamFrame, type WorktreeStrategy,
} from "./api";
import {
  finishRunningTools, newReduceState, reducePart, replayTurns,
  type ChatMessage, type PendingPermission, type PendingQuestion, type ReduceState, type TokenUsage,
} from "./transcript";

/**
 * How long a pending update may wait for a frame that is not coming. Long enough that it never
 * beats `requestAnimationFrame` on a device that is drawing, short enough that an app returning
 * to the foreground is already up to date.
 */
const FLUSH_DEADLINE_MS = 100;

export interface ChatOptions {
  agent: string;
  sessionId: string | null;
  workingDirectory: string;
  workspaceId: string;
  permissionMode: PermissionMode;
  worktreeStrategy?: WorktreeStrategy;
  /** Whether the server says a turn is already running — so an unopened session still streams. */
  running?: boolean;
  /**
   * Whether the connection has settled on an endpoint yet.
   *
   * Not a nicety. Pairing reads the keychain and then races several addresses, so for the first
   * moments after a cold start there is no endpoint to fetch from — and a screen opened straight
   * from a notification or a deep link mounts inside exactly that window. Loading history
   * regardless meant a conversation that exists, on a machine that is reachable, reporting that
   * this phone is not paired.
   */
  ready: boolean;
}

export function useChat(options: ChatOptions) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [usage, setUsage] = useState<TokenUsage | null>(null);
  const [permission, setPermission] = useState<PendingPermission | null>(null);
  const [question, setQuestion] = useState<PendingQuestion | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(options.sessionId);
  const [streaming, setStreaming] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(!!options.sessionId);
  const [historyError, setHistoryError] = useState("");
  const [queued, setQueued] = useState<string[]>([]);
  const [hasOlder, setHasOlder] = useState(false);

  // Every ref in one place, above everything that reads or writes one. Scattering them beside
  // their first use put several below the callback that assigns them, which reads as a cycle.
  const state = useRef<ReduceState>(newReduceState());
  const attachment = useRef<{ abort: () => void } | null>(null);
  const pending = useRef<{ frame: number; timer: ReturnType<typeof setTimeout> } | null>(null);
  const stoppedByUser = useRef(false);
  const olderCursor = useRef<number | null>(null);
  const loadingOlder = useRef(false);
  const pendingQueue = useRef<string[]>([]);
  const runLatest = useRef<((text: string) => Promise<void>) | null>(null);
  /** Whether a turn has been sent from this screen, which decides whose transcript wins. */
  const drove = useRef(false);
  // The latest options, for the callbacks that fire long after the render that created them.
  // Written in an effect: assigning during render is a value a discarded render could leave
  // behind, and nothing here reads it while rendering.
  const settings = useRef(options);
  useEffect(() => { settings.current = options; });

  const cancelPending = useCallback(() => {
    const scheduled = pending.current;
    if (scheduled === null) return;
    // Cleared, not merely cancelled. `flush` reads a non-null handle as "a flush is already on
    // its way", so leaving a dead one in place silently drops every update after it.
    pending.current = null;
    cancelAnimationFrame(scheduled.frame);
    clearTimeout(scheduled.timer);
  }, []);

  /**
   * Copy the mutable transcript into React, once a frame at most. The arrays are rebuilt rather
   * than mutated in place so React sees a new identity; the messages inside keep theirs, so a
   * row whose text did not change does not re-render.
   */
  const flush = useCallback(() => {
    if (pending.current !== null) return;

    const apply = () => {
      cancelPending();
      const current = state.current;
      setMessages([...current.messages]);
      setUsage(current.usage);
      setPermission(current.permission);
      setQuestion(current.question);
    };

    // Two schedulers for one update, and the second is not belt-and-braces.
    //
    // A frame is the right moment to paint — it is never more often than the device draws, which
    // is the whole reason this coalescing exists. But `requestAnimationFrame` only fires while
    // something is being drawn, and an app in a pocket is not. A turn that arrives while the
    // phone is asleep would schedule a frame that never comes, and because a pending handle also
    // means "do not schedule another", every later update would be dropped behind it — a
    // transcript frozen at whatever was on screen when the app went away.
    //
    // So: a frame if one comes, and a timer if it does not. Whichever fires first cancels the
    // other, and the update lands either way.
    pending.current = {
      frame: requestAnimationFrame(apply),
      timer: setTimeout(apply, FLUSH_DEADLINE_MS),
    };
  }, [cancelPending]);

  useEffect(() => () => {
    cancelPending();
    attachment.current?.abort();
  }, [cancelPending]);

  // ── History ────────────────────────────────────────────────────────────────────────────────

  /**
   * Load the newest page of a conversation.
   *
   * Deliberately does not raise the spinner itself. `loadingHistory` starts true whenever this
   * hook was given a session, so there is nothing to raise on the first load — and setting it
   * from inside an effect would be a render that immediately schedules another. A retry, which
   * comes from a person pressing a button rather than from an effect, raises it itself.
   */
  const loadHistory = useCallback(async (id: string) => {
    try {
      const page = await fetchSessionTurnsPage(id);
      // Only the newest page, and deliberately: a phone scrolls a conversation, it does not
      // audit one, and pulling every page of a long session over a mobile network to render
      // above the fold is a cost with no reader. `loadOlder` is there for whoever wants it.
      state.current = replayTurns(page.turns);
      olderCursor.current = page.next_before_row_id;
      setHasOlder(page.has_more);
      flush();
    } catch (caught) {
      setHistoryError(caught instanceof Error ? caught.message : "Could not load this conversation.");
    } finally {
      setLoadingHistory(false);
    }
  }, [flush]);

  const loadOlder = useCallback(async () => {
    const id = sessionId;
    if (!id || loadingOlder.current || olderCursor.current === null) return;
    loadingOlder.current = true;
    try {
      const page = await fetchSessionTurnsPage(id, olderCursor.current);
      const older = replayTurns(page.turns);
      // Older keys are re-prefixed so they cannot collide with the ones already on screen —
      // both replays counted from one.
      const shifted = older.messages.map((message) => ({ ...message, key: `old:${olderCursor.current}:${message.key}` }));
      const current = state.current;
      // Replaced whole rather than written field by field, because every recorded *position*
      // moves by however many rows went in front of it — the open paragraph, the open thinking
      // row, and each live tool call.
      state.current = {
        ...current,
        messages: [...shifted, ...current.messages],
        lane: current.lane === null ? null : current.lane + shifted.length,
        thinking: current.thinking === null ? null : current.thinking + shifted.length,
        tools: new Map([...current.tools].map(([call, index]) => [call, index + shifted.length])),
      };
      olderCursor.current = page.next_before_row_id;
      setHasOlder(page.has_more);
      flush();
    } catch {
      // An older page that will not load is not worth a message: what is on screen still is.
    } finally {
      loadingOlder.current = false;
    }
  }, [sessionId, flush]);

  // Load once the machine is reachable, and once only.
  //
  // There is no reset here, and no re-initialising when the session changes: the conversation
  // screen mounts one of these per route, so a different conversation is a different hook with
  // its own initial state rather than this one being emptied and refilled. That is why the
  // effect can be a plain guard.
  const loadedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!options.sessionId || !options.ready) return;
    if (loadedFor.current === options.sessionId) return;
    loadedFor.current = options.sessionId;
    void loadHistory(options.sessionId);
  }, [options.sessionId, options.ready, loadHistory]);

  // ── The stream ─────────────────────────────────────────────────────────────────────────────

  const settle = useCallback(() => {
    // The attach stream is deliberately *not* aborted when a gate is outstanding. A suspended
    // turn ends and then resumes on the same session, and a client that stopped listening at the
    // suspension would answer the approval and then watch nothing happen.
    const parked = state.current.permission !== null || state.current.question !== null;
    if (!parked) {
      attachment.current?.abort();
      attachment.current = null;
    }
    finishRunningTools(state.current);
    setStreaming(false);
    flush();

    const next = pendingQueue.current.shift();
    // A queue the user stopped is left alone: pressing Stop means stop, not "stop this one and
    // start the next".
    if (next !== undefined && !stoppedByUser.current) {
      setQueued([...pendingQueue.current]);
      // Through a ref, because these two call each other: a turn ending starts the next queued
      // one, and a turn starting is what eventually ends. Naming `run` here directly would be
      // reading it before it is declared.
      void runLatest.current?.(next);
    } else if (next !== undefined) {
      pendingQueue.current.unshift(next);
    }
    stoppedByUser.current = false;
  }, [flush]);

  const consume = useCallback((incoming: SessionStreamFrame) => {
    if (incoming.kind === "live") {
      reducePart(state.current, incoming.part);
      flush();
      return;
    }
    if (incoming.kind === "turn") {
      if (incoming.running) setStreaming(true);
      else settle();
      return;
    }
    if (incoming.kind === "done") settle();
    // `snapshot` is the catch-up frame for a viewer joining mid-turn. The driver's own state is
    // already ahead of it, so it is ignored here and used only by the watcher below.
  }, [flush, settle]);

  const run = useCallback(async (text: string) => {
    const current = settings.current;
    stoppedByUser.current = false;
    drove.current = true;
    setStreaming(true);

    // Optimistic, and in the same paint as the keystroke: creating a session and attaching to it
    // is a round trip, and a composer that empties into nothing for half a second reads as a
    // message that was lost.
    state.current.messages = [
      ...state.current.messages,
      { key: `sent-${Date.now()}`, role: "user", text },
    ];
    flush();

    let id = sessionId;
    try {
      if (!id) {
        const created = await sessionCreate({
          agent: current.agent,
          workingDirectory: current.workingDirectory,
          worktreeStrategy: current.worktreeStrategy ?? "none",
          permissionMode: current.permissionMode,
          workspaceId: current.workspaceId,
        });
        id = created.id;
        setSessionId(id);
      }
      attachment.current?.abort();
      attachment.current = attachSession(id, consume, () => {});
      await sessionSend(id, messageParts(text), {});
    } catch (caught) {
      state.current.messages = [...state.current.messages, {
        key: `error-${Date.now()}`,
        role: "error",
        title: "That message did not send",
        message: caught instanceof Error ? caught.message : String(caught),
        code: "",
      }];
      setStreaming(false);
      flush();
    }
  }, [sessionId, consume, flush]);

  useEffect(() => { runLatest.current = run; }, [run]);

  /**
   * Watch a session this client is not driving — one started at the desk, by a schedule, or on
   * another phone. The same stream, read rather than written, which is what lets somebody open
   * Frank on the way home and see the turn they started that morning still running.
   *
   * Attached whenever there is a session and this client is not already driving one, rather than
   * only when the server says a turn is in flight. Two states made that condition wrong. A
   * session parked on an approval reports itself `waiting`, not `working` — so answering the
   * approval resumed the turn on the machine and nothing arrived on the phone. And an idle
   * session is one message away from working, sent from anywhere, which is the case this whole
   * screen exists for.
   */
  useEffect(() => {
    if (streaming || !sessionId || !options.ready) return;
    const watcher = attachSession(sessionId, (incoming) => {
      // The catch-up frame. Adopted only by a client that has not driven a turn here: our own
      // transcript is ahead of the server's compacted view, and replacing it would throw away
      // the turn we just watched arrive.
      if (incoming.kind === "snapshot") {
        if (!drove.current) {
          state.current = replayTurns(incoming.turns);
          flush();
        }
        return;
      }
      consume(incoming);
    }, () => {});
    return () => watcher.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, streaming, options.ready]);

  // ── What a screen calls ────────────────────────────────────────────────────────────────────

  const send = useCallback((text: string, queueOnly = false) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    if (!streaming) {
      void run(trimmed);
      return;
    }
    if (queueOnly) {
      // A message typed while the session is parked on a decision cannot steer anything — the
      // worker is not reading. It waits for the turn that answering will resume.
      pendingQueue.current.push(trimmed);
      setQueued([...pendingQueue.current]);
      return;
    }
    // Mid-turn, plain text is injected at the next tool boundary rather than queued, which is
    // what makes "no, use the other file" arrive in time to matter.
    const id = sessionId;
    if (!id) return;
    state.current.messages = [
      ...state.current.messages,
      { key: `steer-${Date.now()}`, role: "steering", text: trimmed },
    ];
    flush();
    void sessionSend(id, messageParts(trimmed), {});
  }, [streaming, sessionId, run, flush]);

  const stop = useCallback(async () => {
    if (!sessionId) return;
    stoppedByUser.current = true;
    await cancelTurn(sessionId);
  }, [sessionId]);

  const answerPermission = useCallback(async (decision: "allow_once" | "deny") => {
    const pending = state.current.permission;
    if (!pending || !sessionId) return;
    state.current.permission = null;
    flush();
    await resolvePermissionCall(sessionId, pending.requestId, decision);
  }, [sessionId, flush]);

  const answerQuestion = useCallback(async (answers: unknown[], declined = false) => {
    const pending = state.current.question;
    if (!pending || !sessionId) return;
    state.current.question = null;
    flush();
    await resolveQuestionCall(sessionId, pending.requestId, answers, declined);
  }, [sessionId, flush]);

  const compact = useCallback(async () => {
    if (sessionId) await compactSession(sessionId);
  }, [sessionId]);

  const dropQueued = useCallback((index: number) => {
    pendingQueue.current.splice(index, 1);
    setQueued([...pendingQueue.current]);
  }, []);

  return {
    messages, usage, permission, question, sessionId, streaming, queued,
    loadingHistory, historyError, hasOlder,
    send, stop, answerPermission, answerQuestion, compact, dropQueued, loadOlder,
    retryHistory: () => {
      if (!sessionId) return;
      setLoadingHistory(true);
      setHistoryError("");
      void loadHistory(sessionId);
    },
  };
}
