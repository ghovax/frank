/**
 * Reading the message catalogue without an i18n framework.
 *
 * The desktop reaches these strings through `next-intl`, which it should keep — it has the
 * locale switching, the plural rules and the ICU formatting already. The phone has none of that
 * and does not need it yet, but it does need *the same words*: the whole reason a client invents
 * a label is that reaching the real one was inconvenient, and that is how "Medium risk" became a
 * lowercase `medium` badge on one of the two screens.
 *
 * So this is the smallest thing that makes the catalogue reachable from anywhere: a namespace, a
 * key, and `{placeholder}` substitution. Anything richer than that — plurals, dates, lists — is
 * `next-intl`'s job on the desktop and should come here properly when the phone needs it, rather
 * than being half-implemented now.
 */

import en from "./messages/en.json";

export type Messages = typeof en;
export type Namespace = keyof Messages;

const CATALOGUE: Record<string, Record<string, string>> = en as never;

/**
 * A reader scoped to one namespace, shaped like `useTranslations` so a component reads the same
 * either side.
 *
 * A missing key returns the key itself rather than throwing. A label is not worth a crash, and a
 * key showing through on screen names exactly what is missing — which is more use than an empty
 * string, and considerably more than a blank space where a word should be.
 */
export function labels(namespace: Namespace | string) {
  const entries = CATALOGUE[namespace as string] ?? {};
  return (key: string, values?: Record<string, string | number>): string => {
    const template = entries[key];
    if (typeof template !== "string") return key;
    if (!values) return template;
    return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
      (name in values ? String(values[name]) : whole));
  };
}

/** One string, when reaching for a reader would be more ceremony than the call is worth. */
export function label(namespace: Namespace | string, key: string, values?: Record<string, string | number>): string {
  return labels(namespace)(key, values);
}
