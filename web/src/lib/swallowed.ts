/**
 * Reporting an error that is being handled rather than propagated.
 *
 * This exists because of a specific, expensive failure. The interface had 113 places that
 * caught an error and discarded it — `catch {}` and `.catch(() => {})` — and not one
 * `console.error` among them. When the live stream broke, the browser was receiving the
 * answer, failing to apply it, and saying nothing; the investigation went to the daemon log,
 * which was correct and therefore useless. Four separate faults were stacked behind that
 * silence, each hidden by the one in front.
 *
 * Two ways to not propagate an error, and they are different:
 *
 * - `expected(...)` — the failure is a normal outcome. A cancelled fetch, a probe for a
 *   feature that may not be there, a clipboard read the user declined. Nothing is reported;
 *   the call documents *why* silence is right, which a bare `catch {}` cannot.
 * - `swallowed(...)` — the failure is not expected, but this code can carry on without it.
 *   Reported to the daemon, which forwards it to the OTLP collector already carrying the
 *   harness's traces and metrics.
 *
 * **Why the daemon and not the collector.** The OTLP endpoint and its headers are user
 * configuration living in the daemon. A browser talking to the collector directly would mean
 * shipping those credentials into a page and negotiating CORS with someone else's backend.
 * One configured endpoint, three streams, and nothing sensitive in the webview.
 *
 * The transport is *installed* rather than imported: `api.ts` owns endpoint resolution and
 * the capability token, and it already imports this module, so reaching back for its fetch
 * would be a cycle. It hands the sender in at load instead, which also keeps this module
 * dependency-free and therefore safe to call from anywhere, including from inside `api.ts`.
 *
 * Batched and fire-and-forget: a fault report must never delay the thing that survived the
 * fault, and must never fail loudly enough to become a second fault. The console keeps a copy
 * because when telemetry is unconfigured — the local-first default — there is nowhere to send
 * and a developer still has to be able to see it.
 */

interface ClientFault {
  context: string;
  detail: string;
  url: string;
  sessionId: string;
}

type FaultSender = (faults: ClientFault[]) => Promise<unknown>;

let send: FaultSender | null = null;

/** Install the transport. Called once by `api.ts`, which owns the endpoint and the token. */
export function setFaultSender(sender: FaultSender): void {
  send = sender;
}

const PENDING: ClientFault[] = [];
// Enough to ride out a burst (a failing poll fires repeatedly) without growing without bound
// if the daemon is the thing that is down.
const MAX_PENDING = 64;
const FLUSH_DELAY_MS = 2000;
let flushTimer: ReturnType<typeof setTimeout> | null = null;

function detailOf(error: unknown): string {
  if (error instanceof Error) return error.stack || `${error.name}: ${error.message}`;
  return String(error);
}

async function flush(): Promise<void> {
  flushTimer = null;
  if (PENDING.length === 0) return;
  const faults = PENDING.splice(0, PENDING.length);
  if (send === null) return;
  try {
    await send(faults);
  } catch {
    // Deliberately terminal. Reporting that we could not report is a loop, and re-queuing
    // would keep a dead daemon's worth of faults alive forever. The console copy below was
    // already written, so nothing is lost that was not already visible.
  }
}

/** A failure this code can continue past, but which nobody chose. Reported. */
export function swallowed(context: string, error: unknown): void {
  const detail = detailOf(error);
  // Written first and unconditionally: this is what a developer reads when telemetry is off,
  // which is the default, and what survives if the daemon is the thing that broke.
  console.error(`[frank] ${context}:`, error);
  if (typeof window === "undefined") return;
  if (PENDING.length >= MAX_PENDING) return;
  PENDING.push({
    context,
    detail,
    url: window.location?.pathname ?? "",
    sessionId: new URLSearchParams(window.location?.search ?? "").get("session") ?? "",
  });
  if (flushTimer === null) flushTimer = setTimeout(() => void flush(), FLUSH_DELAY_MS);
}

/**
 * A failure that is a normal outcome here. Silent by design.
 *
 * `why` is not used at runtime and is not decoration: it is the difference between "this was
 * considered" and "this was ignored", which is exactly what a bare `catch {}` destroys.
 */
export function expected(_why: string, _error?: unknown): void {
  // Nothing. The argument is the documentation.
}
