/**
 * An identifier this client makes up for itself.
 *
 * `crypto.randomUUID` is the obvious way and it is not always there. It is restricted to secure
 * contexts, and the interface reaches a phone over plain HTTP from a private address — where it
 * is not refused but simply absent, so calling it throws "randomUUID is not a function" and
 * takes the message being sent down with it.
 *
 * `crypto.getRandomValues` carries no such restriction and is present in every context that has
 * a `crypto` at all, so the fallback is a real version-4 UUID rather than a weaker sort of
 * identifier: same shape, same randomness, one line further down. `Math.random` is under that
 * again for a runtime with no `crypto` whatsoever, which nothing this ships to has — it is there
 * so that the last thing between somebody and a sent message is never an exception.
 */
export function clientIdentifier(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    // The two fields a version-4 UUID does not leave to chance: the version nibble, and the two
    // top bits of the variant.
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
