/** Reading the message catalogue without an i18n framework, for the surfaces that have none. */

import en from "./messages/en.json";

export type Messages = typeof en;
export type Namespace = keyof Messages;

const CATALOGUE: Record<string, Record<string, string>> = en as never;

/** A reader scoped to one namespace, shaped like `useTranslations`, answering a missing key with the key. */
export function labels(namespace: Namespace | string) {
  const entries = CATALOGUE[namespace as string] ?? {};
  return (key: string, values?: Record<string, string | number>): string => {
    const template = entries[key];
    if (typeof template !== "string") return key;
    if (!values) return template;
    return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
      name in values ? String(values[name]) : whole,
    );
  };
}

/** One string, when reaching for a reader would be more ceremony than the call is worth. */
export function label(
  namespace: Namespace | string,
  key: string,
  values?: Record<string, string | number>,
): string {
  return labels(namespace)(key, values);
}
