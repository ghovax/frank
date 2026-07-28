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
 *   feature that may not be there, a clipboard read the user declined. Nothing is written;
 *   the call documents *why* silence is right, which a bare `catch {}` cannot.
 * - `swallowed(...)` — the failure is not expected, but this code can carry on without it.
 *   Written to the console with a stable `[frank]` prefix and the context that names where it
 *   happened, so it is findable without a reproduction.
 *
 * The rule is that neither takes an empty body. If you cannot say which of the two it is, it
 * is `swallowed`.
 */

/** A failure this code can continue past, but which nobody chose. Reported. */
export function swallowed(context: string, error: unknown): void {
  console.error(`[frank] ${context}:`, error);
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
