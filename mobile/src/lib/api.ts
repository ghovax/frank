/** Asking a machine whether it is there, which is the whole of this file. */

import { fetch as streamingFetch } from "expo/fetch";

export type PermissionMode = "ask" | "automatic";
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

/** A failed request, named by its catalogue key rather than a sentence this module cannot translate. */
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

/** One door: every request carries the reach token as a header. */
export async function apiFetch(path: string, options: RequestOptions = {}) {
  if (!endpoint) throw new ApiError("notPaired", 0, "unpaired");
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
  if (response.status === 401) throw new ApiError("noLongerPaired", 401, "unauthorized");
  const data = await response.json();
  if (data?.error) {
    throw new ApiError(String(data.error.message ?? "daemonRefused"), response.status, String(data.error.code ?? ""));
  }
  return data.result as T;
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
