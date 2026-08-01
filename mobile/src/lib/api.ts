/**
 * The daemon, as this app addresses it.
 *
 * A transcription of `web/src/lib/api.ts`, narrowed to what a phone does and changed in exactly
 * two places:
 *
 *   - **Where it points.** The web client discovers a loopback port; this one is handed an
 *     endpoint and a token by pairing, so `configure()` replaces five layers of discovery.
 *   - **How it streams.** `fetch` in React Native resolves its whole body before it answers, so
 *     `response.body` is not a stream and an SSE tail would arrive when the turn was over.
 *     `expo/fetch` is the WinterCG implementation that does stream, and every call goes through
 *     it — one door rather than two, so nothing has to remember which kind of request it is.
 *
 * Everything else — the RPC envelope, the A2A part shapes, the SSE frame parser, the
 * reconnecting subscription — is the same code doing the same thing, and is meant to stay
 * comparable to the file it came from.
 */

import { fetch as streamingFetch } from "expo/fetch";

export type PermissionMode = "default" | "auto" | "read_only";
export type WorktreeStrategy = "none" | "branch" | "worktree";

/** Where this app is pointed, and what proves it may be. Set by the connection layer. */
let endpoint = "";
let bearer = "";

export function configure(base: string, token: string): void {
  endpoint = base.replace(/\/+$/, "");
  bearer = token;
}

export function currentEndpoint(): string {
  return endpoint;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code = "") {
    super(message);
    this.name = "ApiError";
  }
}

interface RequestOptions {
  method?: string;
  body?: string | ArrayBuffer;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

/**
 * One door. Every request carries the reach token as a header; the query-parameter form the web
 * client also needs is for transports that cannot carry headers, and this app has none of those
 * that are not the websocket below.
 */
export async function apiFetch(path: string, options: RequestOptions = {}) {
  if (!endpoint) throw new ApiError("Not paired with a Frank yet.", 0, "unpaired");
  return streamingFetch(`${endpoint}${path}`, {
    method: options.method ?? "GET",
    headers: { Authorization: `Bearer ${bearer}`, ...(options.headers ?? {}) },
    body: options.body as never,
    signal: options.signal,
  });
}

/** A URL something other than `apiFetch` will open — a websocket, or an `<Image>` source. */
export function withToken(path: string): string {
  const separator = path.includes("?") ? "&" : "?";
  return `${endpoint}${path}${separator}token=${encodeURIComponent(bearer)}`;
}

/** The control plane: one method name and its parameters, over one route. */
export async function rpc<T>(method: string, params: Record<string, unknown> = {}, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch("/rpc", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method, params }),
    signal,
  });
  if (response.status === 401) throw new ApiError("This device is no longer paired.", 401, "unauthorized");
  const data = await response.json();
  if (data?.error) {
    throw new ApiError(String(data.error.message ?? "The daemon refused."), response.status, String(data.error.code ?? ""));
  }
  return data.result as T;
}

// ── A2A shapes ────────────────────────────────────────────────────────────────────────────────

export type A2APartKind = "text" | "data" | "file";

export interface A2APart {
  kind: A2APartKind;
  text?: string;
  data?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

/**
 * A turn's control state lives under one URI-namespaced key, which is A2A's convention for an
 * extension's attributes. The same key wraps the harness's payload inside a `DataPart`, because
 * it is the same extension.
 */
export const TURN_STATE_KEY = "urn:frank:ext:turn:v1";

export type TurnKind = "user" | "peer" | "autonomous" | "compaction";

export interface TurnState {
  kind?: TurnKind;
  peerSender?: string;
  referenceTurnIds?: string[];
}

export function partPayload(data: Record<string, unknown> | undefined): Record<string, unknown> {
  const payload = data?.[TURN_STATE_KEY];
  return payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
}

export function wrapPartPayload(payload: Record<string, unknown>): Record<string, unknown> {
  return { [TURN_STATE_KEY]: payload };
}

export function turnState(turn: { metadata?: Record<string, unknown> } | undefined): TurnState {
  const state = turn?.metadata?.[TURN_STATE_KEY];
  return state && typeof state === "object" ? (state as TurnState) : {};
}

export interface A2AMessage {
  role: "user" | "agent";
  parts: A2APart[];
  messageId?: string;
  metadata?: Record<string, unknown>;
}

export interface A2AArtifact {
  artifactId?: string;
  name?: string;
  parts: A2APart[];
}

export interface A2ATurn {
  id: string;
  contextId: string;
  status: { state: string; message?: A2AMessage; timestamp?: string };
  artifacts?: A2AArtifact[];
  history?: A2AMessage[];
  metadata?: Record<string, unknown>;
}

/** The parts a turn carries: typed prose, plus any structured payloads as DataParts. */
export function messageParts(text: string, dataParts?: Record<string, unknown>[]): A2APart[] {
  const parts: A2APart[] = [];
  if (text) parts.push({ kind: "text", text });
  for (const dataPart of dataParts ?? []) parts.push({ kind: "data", data: wrapPartPayload(dataPart) });
  if (parts.length === 0) parts.push({ kind: "text", text: "" });
  return parts;
}

// ── Sessions ──────────────────────────────────────────────────────────────────────────────────

export interface SessionSummary {
  id: string;
  agent: string;
  parent: string;
  lifecycle: "live" | "ended";
  activity: "working" | "waiting" | "idle" | "asleep" | "ended";
  outcome: string;
  awaiting_input: boolean;
  title: string;
  working_directory: string;
  workspace_id: string;
  permission_mode: PermissionMode;
  created_at: string;
  updated_at: string;
  exit_reason: string;
}

export async function fetchSessions(all = false): Promise<SessionSummary[]> {
  const data = await rpc<{ sessions?: SessionSummary[] }>("session.list", all ? { all: true } : {});
  return data.sessions ?? [];
}

export async function fetchSession(sessionId: string): Promise<SessionSummary | null> {
  if (!sessionId) return null;
  const data = await rpc<{ session: SessionSummary }>("session.get", { id: sessionId });
  return data.session ?? null;
}

export interface SessionCreateInput {
  agent: string;
  workingDirectory?: string;
  worktreeStrategy?: WorktreeStrategy;
  permissionMode?: PermissionMode;
  workspaceId?: string;
}

export interface SessionCreated {
  id: string;
  token: string;
  agent?: string;
  parent?: string;
  permission_mode?: PermissionMode;
}

/**
 * The one place a session's agent, directory, workspace and permission mode are fixed. `send`
 * never changes any of them — which is why the composer's selectors are disabled once a session
 * has turns in it.
 */
export async function sessionCreate(input: SessionCreateInput): Promise<SessionCreated> {
  return rpc<SessionCreated>("session.create", {
    agent: input.agent,
    working_directory: input.workingDirectory ?? "",
    worktree_strategy: input.worktreeStrategy ?? "none",
    permission_mode: input.permissionMode ?? "default",
    workspace_id: input.workspaceId ?? "",
  });
}

export async function sessionSend(
  sessionId: string, parts: A2APart[], metadata: Record<string, unknown> = {},
): Promise<string> {
  const data = await rpc<{ task_id?: string }>("session.send", { id: sessionId, parts, metadata });
  return data.task_id ?? "";
}

export async function setSessionPermissionMode(sessionId: string, mode: PermissionMode): Promise<PermissionMode> {
  const data = await rpc<{ permission_mode?: PermissionMode }>("session.permission_mode", {
    id: sessionId, permission_mode: mode,
  });
  return data.permission_mode ?? mode;
}

export interface SessionTurnsPage {
  turns: A2ATurn[];
  next_before_row_id: number | null;
  has_more: boolean;
}

export async function fetchSessionTurnsPage(
  sessionId: string, beforeRowId?: number | null, signal?: AbortSignal, limit = 200,
): Promise<SessionTurnsPage> {
  const data = await rpc<{ turns?: A2ATurn[]; next_before_row_id?: number | null; has_more?: boolean }>(
    "session.history",
    { id: sessionId, limit, ...(beforeRowId != null ? { before_row_id: beforeRowId } : {}) },
    signal,
  );
  return {
    turns: data.turns ?? [],
    next_before_row_id: data.next_before_row_id ?? null,
    has_more: !!data.has_more,
  };
}

export async function cancelTurn(sessionId: string): Promise<boolean> {
  try {
    await rpc("turn.cancel", { id: sessionId });
    return true;
  } catch {
    return false;
  }
}

export async function deleteSession(sessionId: string): Promise<boolean> {
  try {
    await rpc("session.end", { id: sessionId });
    return true;
  } catch {
    return false;
  }
}

export async function compactSession(sessionId: string): Promise<boolean> {
  try {
    await rpc("session.compact", { id: sessionId });
    return true;
  } catch {
    return false;
  }
}

// ── Answering a gate ──────────────────────────────────────────────────────────────────────────

export interface ResolveResult {
  ok: boolean;
  status: "resolved" | "stale" | "error";
}

async function sessionRespond(payload: Record<string, unknown>): Promise<ResolveResult> {
  try {
    const data = await rpc<{ resolved?: unknown }>("session.respond", payload);
    // `false` is not a failure: the session had no such request pending, because the turn moved
    // on or the gate was answered twice. Nothing is wrong, so the card settles quietly.
    return { ok: true, status: data.resolved === true ? "resolved" : "stale" };
  } catch {
    return { ok: false, status: "error" };
  }
}

export async function resolvePermission(
  sessionId: string, requestId: string, decision: "deny" | "allow_once",
): Promise<ResolveResult> {
  return sessionRespond({ id: sessionId, request_id: requestId, decision });
}

export async function resolveQuestion(
  sessionId: string, requestId: string, answers: unknown[], declined = false,
): Promise<ResolveResult> {
  return sessionRespond({ id: sessionId, request_id: requestId, answers, declined });
}

// ── Workspaces, agents, settings ──────────────────────────────────────────────────────────────

export interface WorkspaceLocation {
  id: string;
  name: string;
  kind: "local" | "remote";
  host_alias: string;
  base_directory: string;
}

export interface Workspace {
  id: string;
  locations: WorkspaceLocation[];
  session_count: number;
  updated_at: string;
}

export async function listWorkspaces(): Promise<Workspace[]> {
  const response = await apiFetch("/workspaces");
  const data = await response.json();
  return (data.workspaces ?? []) as Workspace[];
}

export interface AgentSummary {
  id: string;
  name: string;
  title: string;
  description: string;
  model: string;
}

export async function fetchAgents(workingDirectory = ""): Promise<AgentSummary[]> {
  const response = await apiFetch(`/agents?working_directory=${encodeURIComponent(workingDirectory)}`);
  const data = await response.json();
  return (data.agents ?? []) as AgentSummary[];
}

export interface Settings {
  permission_mode: PermissionMode;
  dictation_enabled: boolean;
  computer_control_enabled: boolean;
  user_context_enabled: boolean;
  /**
   * What confines a session's tool children on that machine, and what it is called. The daemon
   * answers with `{backend, detail}` rather than a name — `backend` is empty when nothing on the
   * host can enforce it, which is a different thing from confinement being switched off.
   */
  sandbox_backend: { backend: string; detail: string };
  /** The whole confinement policy. `enforce` is what says whether it is actually applied. */
  sandbox: { enforce: string };
  worktree_strategy: WorktreeStrategy;
}

export async function fetchSettings(): Promise<Settings> {
  const response = await apiFetch("/settings");
  return (await response.json()) as Settings;
}

/**
 * The three booleans a phone can sensibly change, each with its own endpoint on the daemon.
 *
 * Separate calls rather than a `POST /settings` with the whole object, which is what the desktop
 * does too: turning dictation off also releases the model that machine was holding, and a
 * whole-object write would carry along whatever the phone last read for everything else.
 */
export async function updateDictationSetting(enabled: boolean): Promise<void> {
  await postSetting("/settings/dictation", { enabled });
}

export async function updateComputerControlSetting(enabled: boolean): Promise<void> {
  await postSetting("/settings/computer-control", { enabled });
}

export async function updateUserContextSetting(enabled: boolean): Promise<void> {
  await postSetting("/settings/user-context", { enabled });
}

async function postSetting(path: string, body: Record<string, unknown>): Promise<void> {
  const response = await apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(`That setting would not save (${response.status}).`, response.status);
}

// ── Schedules ─────────────────────────────────────────────────────────────────────────────────

export interface Schedule {
  id: string;
  workspace_id: string;
  name: string;
  cron: string;
  timezone: string;
  agent: string;
  prompt: string;
  permission_mode: PermissionMode;
  working_directory: string;
  enabled: boolean;
  last_fired_at: string;
  last_error: string;
  next_firing: string;
}

export async function listSchedules(workspaceId = ""): Promise<Schedule[]> {
  const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : "";
  const response = await apiFetch(`/schedules${query}`);
  const data = await response.json();
  return (data.schedules ?? []) as Schedule[];
}

export async function setScheduleEnabled(id: string, enabled: boolean): Promise<void> {
  await apiFetch(`/schedules/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export async function runSchedule(id: string): Promise<void> {
  await apiFetch(`/schedules/${encodeURIComponent(id)}/run`, { method: "POST" });
}

export async function deleteSchedule(id: string): Promise<void> {
  await apiFetch(`/schedules/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// ── Dictation ─────────────────────────────────────────────────────────────────────────────────

export const DICTATION_SAMPLE_RATE = 16000;

export type DictationState = "idle" | "loading" | "ready" | "failed";

export interface DictationStatus {
  enabled: boolean;
  model: string;
  state: DictationState;
  failure: string;
}

export async function fetchDictationStatus(prepare = false): Promise<DictationStatus> {
  const response = await apiFetch(`/dictation${prepare ? "?prepare=true" : ""}`);
  const data = await response.json();
  return {
    enabled: !!data.enabled,
    model: String(data.model ?? ""),
    state: (data.state ?? "idle") as DictationState,
    failure: String(data.failure ?? ""),
  };
}

/**
 * Transcribe one recording, on the machine Frank runs on. The body is raw mono float32 at
 * 16 kHz — no codec on either side, which is what makes this the one endpoint a phone can use
 * unchanged despite having a completely different recorder.
 */
export async function transcribeDictation(samples: Float32Array): Promise<string> {
  const response = await apiFetch("/dictation/transcribe", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: samples.buffer.slice(samples.byteOffset, samples.byteOffset + samples.byteLength) as ArrayBuffer,
  });
  if (!response.ok) {
    // The server's own sentence is worth lifting out: it distinguishes a missing package from a
    // failed download from a hung worker, and only it knows which happened.
    let detail = "";
    try {
      detail = String((await response.json())?.detail ?? "");
    } catch {
      // A body that is not JSON says nothing the status did not.
    }
    throw new Error(detail || `Transcription failed (${response.status}).`);
  }
  const data = await response.json();
  return String(data.text ?? "");
}

// ── Server-sent events ────────────────────────────────────────────────────────────────────────

export function parseSseFrame(frame: string): string {
  return frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n")
    .trim();
}

/**
 * Read an SSE body frame by frame. `onFrame` returns "stop" to end the read early — an in-band
 * terminator, which the attach stream uses so the connection closes on `done` rather than being
 * left to time out.
 */
async function pumpEventStream(
  body: ReadableStream<Uint8Array>, onFrame: (raw: string) => void | "stop",
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const raw = parseSseFrame(frame);
      if (!raw) continue;
      if (onFrame(raw) === "stop") return;
    }
    if (done) break;
  }
  const trailing = parseSseFrame(buffer);
  if (trailing) onFrame(trailing);
}

/**
 * A long-lived subscription that reconnects itself. `EventSource` would have done this for free,
 * but it cannot carry an Authorization header, so the retry is ours — one second, flat, because
 * the failure this recovers from is usually a phone changing networks rather than a server that
 * needs time.
 */
function openEventStream(path: string, onData: (raw: string) => void): { close: () => void } {
  const controller = new AbortController();
  let closed = false;

  const run = async () => {
    while (!closed) {
      try {
        const response = await apiFetch(path, {
          signal: controller.signal,
          headers: { Accept: "text/event-stream" },
        });
        if (response.ok && response.body) await pumpEventStream(response.body as ReadableStream<Uint8Array>, onData);
      } catch {
        // A deliberate close and a dropped connection arrive the same way; `closed` tells them
        // apart.
      }
      if (closed) return;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  };
  void run();

  return { close: () => { closed = true; controller.abort(); } };
}

export type BroadcastEvent =
  | "sessions_changed" | "agents_changed" | "settings_changed" | "workspaces_changed"
  | "remote_agents_changed" | "schedules_changed" | "hosts_changed" | "filesystem_leases_changed";

let busSubscribers: ((event: BroadcastEvent) => void)[] = [];
let bus: { close: () => void } | null = null;

/**
 * The daemon-wide invalidation bus, shared. One connection fanned out rather than one per
 * caller: the web client learned this from exhausting a browser's per-host connection limit, and
 * a phone on a mobile network has less headroom, not more.
 */
export function subscribeEvents(onEvent: (event: BroadcastEvent) => void): () => void {
  busSubscribers.push(onEvent);
  if (bus === null) {
    bus = openEventStream("/events", (raw) => {
      let type = "";
      try {
        type = String(JSON.parse(raw)?.type ?? "");
      } catch {
        return;
      }
      if (type) for (const subscriber of busSubscribers) subscriber(type as BroadcastEvent);
    });
  }
  return () => {
    busSubscribers = busSubscribers.filter((entry) => entry !== onEvent);
    if (busSubscribers.length === 0 && bus !== null) {
      bus.close();
      bus = null;
    }
  };
}

/** Dropped when the endpoint changes, so a stream does not outlive the machine it was reading. */
export function closeSharedStreams(): void {
  bus?.close();
  bus = null;
  busSubscribers = [];
}

export type SessionStreamFrame =
  | { kind: "snapshot"; turns: A2ATurn[] }
  | { kind: "live"; seq: number; part: A2APart }
  | { kind: "turn"; seq: number; running: boolean }
  | { kind: "done" };

/**
 * A live view of one session. `turn` and `done` are different events and the difference matters:
 * a session goes idle many times over its life, and `done` is the session itself ending.
 */
export function attachSession(
  sessionId: string,
  onFrame: (frame: SessionStreamFrame) => void,
  onDone: () => void,
): { abort: () => void } {
  const controller = new AbortController();
  apiFetch(`/sessions/${encodeURIComponent(sessionId)}/attach`, {
    signal: controller.signal,
    headers: { Accept: "text/event-stream" },
  })
    .then(async (response) => {
      if (!response.ok || !response.body) return;
      await pumpEventStream(response.body as ReadableStream<Uint8Array>, (raw) => {
        let frame: SessionStreamFrame;
        try {
          frame = JSON.parse(raw) as SessionStreamFrame;
        } catch {
          return;
        }
        onFrame(frame);
        if (frame.kind === "done") {
          controller.abort();
          return "stop";
        }
      });
    })
    .catch(() => {
      // An abort and a dropped connection both end the stream; `onDone` still fires.
    })
    .finally(onDone);

  return { abort: () => controller.abort() };
}

/** Is the machine there, and does this device still count? Both answers in one round trip. */
export async function probe(base: string, token: string, signal?: AbortSignal): Promise<"ok" | "unauthorized" | "unreachable"> {
  try {
    const response = await streamingFetch(`${base.replace(/\/+$/, "")}/health`, {
      headers: { Authorization: `Bearer ${token}` },
      signal,
    });
    if (response.status === 401) return "unauthorized";
    return response.ok ? "ok" : "unreachable";
  } catch {
    return "unreachable";
  }
}
