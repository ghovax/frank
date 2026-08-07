/** Reporting an error that is being handled rather than propagated. */

import { errorFields } from "./errors";

/** Where a fault happened and what was being attempted, as two fields rather than one glued sentence. */
export interface FaultSite {
  /** The surface it happened on, kebab-case and stable, naming the module rather than the file path. */
  component: string;
  /** What was being attempted, as a short verb phrase, never the outcome and never the component again. */
  operation: string;
}

/** Scalar facts about the fault, which become one attribute each rather than a sentence. */
export type FaultDetail = Record<string, string | number | boolean>;

export interface ClientFault {
  component: string;
  operation: string;
  detail: FaultDetail;
  // The error as flat fields rather than one blob, because these become attributes and an attribute is a scalar.
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
export function swallowed(site: FaultSite, error: unknown, detail: FaultDetail = {}): void {
  if (send === null || typeof window === "undefined") return;
  const fields = errorFields(error);
  void send({
    component: site.component,
    operation: site.operation,
    detail,
    errorName: fields.name,
    errorMessage: fields.message,
    errorStack: fields.stack,
    url: window.location?.pathname ?? "",
    sessionId: new URLSearchParams(window.location?.search ?? "").get("session") ?? "",
  }).catch(() => {
    // Deliberately terminal, since reporting that we could not report is a loop.
  });
}

/** A failure that is a normal outcome here, silent by design, with `why` recording that it was considered. */
export function expected(_why: string, _error?: unknown): void {
  // Nothing. The argument is the documentation.
}
