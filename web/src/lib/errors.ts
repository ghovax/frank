import { serializeError } from "serialize-error";

// Turning whatever was thrown into something we can rely on.
//
// JavaScript lets you throw anything, and libraries do. `cronstrue` throws a *string*, so
// `caught instanceof Error` is false and `caught.message` is `undefined` — and the interface
// had thirteen places written as `error instanceof Error ? error.message : ""`, every one of
// which shows a person an error toast with an empty explanation when the thing thrown is not an
// `Error`. That is the failure this module exists to make unreachable: nothing narrows a caught
// value by hand any more.
//
// `serialize-error` does the parsing. It wraps non-`Error` values in a `NonError` and hands
// back a plain object, which is exactly the structure telemetry wants — but two of its results
// need translating before a person sees them, and that is the whole of what is added here:
//
//   • A thrown string comes back as `Non-error value: <the string>`. True, and unreadable in a
//     toast; the string itself is the message somebody should see.
//   • A thrown plain object comes back carrying its own keys and no `message` at all, so a
//     naive read of `.message` is `undefined` a second time, by a different route.

/** An error flattened into fields. Flat, not nested: OTLP attributes are scalars. */
export interface ErrorFields {
  name: string;
  message: string;
  stack: string;
}

/**
 * What was thrown, as fields, for a log or a span. Never empty: something was thrown, so
 * something is always said about it.
 */
export function errorFields(thrown: unknown): ErrorFields {
  const serialized = serializeError(thrown) as Record<string, unknown>;
  const name = typeof serialized.name === "string" && serialized.name ? serialized.name : "NonError";
  const stack = typeof serialized.stack === "string" ? serialized.stack : "";
  return { name, message: errorMessage(thrown), stack };
}

/**
 * What was thrown, as a sentence to show a person. Empty only when there is genuinely nothing
 * to say — never because the thrown value was the wrong shape.
 */
export function errorMessage(thrown: unknown): string {
  // A string was thrown: it *is* the message. Wrapping it in "Non-error value:" would be
  // telling the reader about JavaScript rather than about their problem.
  if (typeof thrown === "string") return thrown.replace(/^Error:\s*/, "").trim();
  if (thrown instanceof Error && thrown.message) return thrown.message;
  const serialized = serializeError(thrown) as Record<string, unknown>;
  if (typeof serialized.message === "string" && serialized.message) return serialized.message;
  // A thrown object with no message of its own. Its own keys are all there is, so they are what
  // gets shown, rather than the empty string thirteen call sites used to produce.
  if (thrown !== null && typeof thrown === "object") {
    try {
      return JSON.stringify(thrown);
    } catch {
      // A cyclic object cannot be rendered; its type is still better than nothing.
      return Object.prototype.toString.call(thrown);
    }
  }
  return thrown === undefined ? "" : String(thrown);
}
