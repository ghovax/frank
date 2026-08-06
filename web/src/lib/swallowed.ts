/** Reporting an error that is being handled rather than propagated. */

import { errorFields } from "./errors";

/** Where a fault happened and what was being attempted, as two fields rather than as one sentence with the place glued to the front. */
export interface FaultSite {
  /** The surface it happened on, kebab-case and stable: `chat-panel`, `pdf-view`, `api`. */
  component: string;
  /** What was being attempted, as a short verb phrase: `load the message history`. */
  operation: string;
}

export interface ClientFault {
  component: string;
  operation: string;
  // The error as fields rather than as one pre-formatted blob.
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
    // Deliberately terminal, and the one place here where discarding is right: reporting that we could not report is a loop.
  });
}

/** A failure that is a normal outcome here. Silent by design. */
export function expected(_why: string, _error?: unknown): void {
  // Nothing. The argument is the documentation.
}
