import { serializeError } from "serialize-error";

// Turning whatever was thrown into something we can rely on.

/** An error flattened into fields. Flat, not nested: OTLP attributes are scalars. */
export interface ErrorFields {
  name: string;
  message: string;
  stack: string;
}

/** What was thrown, as fields, for a log or a span. */
export function errorFields(thrown: unknown): ErrorFields {
  const serialized = serializeError(thrown) as Record<string, unknown>;
  const name = typeof serialized.name === "string" && serialized.name ? serialized.name : "NonError";
  const stack = typeof serialized.stack === "string" ? serialized.stack : "";
  return { name, message: errorMessage(thrown), stack };
}

/** What was thrown, as a sentence to show a person. */
export function errorMessage(thrown: unknown): string {
  // A string was thrown: it *is* the message.
  if (typeof thrown === "string") return thrown.replace(/^Error:\s*/, "").trim();
  if (thrown instanceof Error && thrown.message) return thrown.message;
  const serialized = serializeError(thrown) as Record<string, unknown>;
  if (typeof serialized.message === "string" && serialized.message) return serialized.message;
  // A thrown object with no message of its own.
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
