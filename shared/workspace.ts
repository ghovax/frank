/** What a workspace and a location are called, shared so the phone and the desktop agree. */

export interface LocationName {
  base_directory: string;
  name?: string;
}

/** A location's short name: the last segment of its directory, which is what people call it. */
export function locationTargetLabel(location: LocationName): string {
  const normalizedDirectory = location.base_directory.replace(/\/+$/, "");
  return (
    normalizedDirectory.split("/").pop() ||
    location.name ||
    location.base_directory
  );
}

/** A workspace's name, which is the names of everything in it rather than whichever came first. */
export function workspaceLabel(
  locations: LocationName[] | undefined,
  locale: string,
  fallback: string,
): string {
  const names = (locations ?? []).map(locationTargetLabel).filter(Boolean);
  if (names.length === 0) return fallback;
  try {
    return new Intl.ListFormat(locale, {
      style: "narrow",
      type: "conjunction",
    }).format(names);
  } catch {
    // A locale the platform does not know: the names still matter more than the separator.
    return names.join(", ");
  }
}
