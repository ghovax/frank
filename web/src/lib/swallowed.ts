/**
 * Reporting an error that is being handled rather than propagated.
 *
 * This exists because of a specific, expensive failure. The interface had 113 places that
 * caught an error and discarded it — `catch {}` and `.catch(() => {})` — and not one report
 * among them. When the live stream broke, the browser was receiving the answer, failing to
 * apply it, and saying nothing; the investigation went to the daemon log, which was correct
 * and therefore useless. Four separate faults were stacked behind that silence, each hidden
 * by the one in front.
 *
 * Two ways to not propagate an error, and they are different:
 *
 * - `expected(...)` — the failure is a normal outcome. A cancelled fetch, a probe for a
 *   feature that may not be there, a clipboard read the user declined. Nothing is reported;
 *   the call documents *why* silence is right, which a bare `catch {}` cannot.
 * - `swallowed(...)` — the failure is not expected, but this code can carry on without it.
 *   Reported to the daemon, which writes it to its log and, when an OTLP endpoint is
 *   configured, forwards it to the collector already carrying traces and metrics. It takes a
 *   :interface:`FaultSite` — *where* and *at what* — rather than a sentence; see below.
 *
 * **Why the daemon and not the collector.** The OTLP endpoint and its headers are user
 * configuration living in the daemon. A browser talking to the collector directly would mean
 * shipping those credentials into a page and negotiating CORS with someone else's backend.
 *
 * **One destination.** No console copy: the daemon logs every fault whether or not telemetry
 * is configured, so `frankd.log` is always the answer to "where did that go". Writing it in
 * two places would only make it ambiguous which to trust.
 *
 * **Sent as it happens.** No batching and no delay — a fault is rare, one request is cheap,
 * and a queue only creates the possibility of losing the last one before a reload.
 *
 * The transport is *installed* rather than imported: `api.ts` owns endpoint resolution and
 * the capability token, and it already imports this module, so reaching back for its fetch
 * would be a cycle.
 */

import { errorFields } from "./errors";

/**
 * Where a fault happened and what was being attempted, as two fields rather than as one
 * sentence with the place glued to the front.
 *
 * The prose form drifted into three incompatible shapes across sixty-odd call sites: twenty-six
 * read `<place>: a background load failed`, where the message carries nothing and the place is
 * the only signal; seventeen were a bare `fetchSkills failed`, a camelCase identifier leaking
 * into telemetry with no message at all; the rest were real sentences, some prefixed and some
 * not. Nothing could be grouped by component without matching on a prefix, which is not a query
 * anybody should have to write against their own telemetry.
 *
 * These become span attributes, and an attribute is a *dimension* — something you filter and
 * group by. `frank.client.component` answers "which surface is failing", `frank.client.operation`
 * answers "at what", and neither has to be parsed out of a string that a colon inside a message
 * would take apart wrongly. The daemon reached this conclusion one layer out already, when its
 * log line stopped being `interface fault at %s: %s -- %s` and became fields; this is the same
 * argument applied to the field it receives.
 */
export interface FaultSite {
  /**
   * The surface it happened on, kebab-case and stable: `chat-panel`, `pdf-view`, `api`. The
   * module, not the file path — a rename of the file should not split a year of telemetry.
   */
  component: string;
  /**
   * What was being attempted, as a short verb phrase: `load the message history`. Never the
   * outcome ("failed" is what a fault *is*) and never the component again.
   */
  operation: string;
}

export interface ClientFault {
  component: string;
  operation: string;
  // The error as fields rather than as one pre-formatted blob. Flat because these become OTLP
  // attributes and an attribute is a scalar — and parsed by `serialize-error`, so a thrown
  // string, number or bare object arrives with a name and a message like anything else.
  errorName: string;
  errorMessage: string;
  errorStack: string;
  url: string;
  sessionId: string;
}

type FaultSender = (fault: ClientFault) => Promise<unknown>;

let send: FaultSender | null = null;

/** Install the transport. Called once by `api.ts`, which owns the endpoint and the token. */
export function setFaultSender(sender: FaultSender): void {
  send = sender;
}

/** A failure this code can continue past, but which nobody chose. Reported. */
export function swallowed(site: FaultSite, error: unknown): void {
  if (send === null || typeof window === "undefined") return;
  const fields = errorFields(error);
  void send({
    component: site.component,
    operation: site.operation,
    errorName: fields.name,
    errorMessage: fields.message,
    errorStack: fields.stack,
    url: window.location?.pathname ?? "",
    sessionId: new URLSearchParams(window.location?.search ?? "").get("session") ?? "",
  }).catch(() => {
    // Deliberately terminal, and the one place here where discarding is right: reporting
    // that we could not report is a loop.
  });
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
