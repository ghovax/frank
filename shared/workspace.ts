/**
 * What a workspace and a location are called.
 *
 * Lifted out of `web/src/components/location-status.tsx`, where it was the desktop's alone. The
 * phone had a second implementation that rendered `giovannigravili +1` — which is not a name, it
 * is a count of the names it decided not to show.
 */

export interface LocationName {
  base_directory: string;
  name?: string;
}

/** A location's short name: the last segment of its directory, which is what people call it. */
export function locationTargetLabel(location: LocationName): string {
  const normalizedDirectory = location.base_directory.replace(/\/+$/, "");
  return normalizedDirectory.split("/").pop() || location.name || location.base_directory;
}

/**
 * A workspace's name, which is the names of everything in it.
 *
 * It used to be `locations[0]`, so a workspace spanning a checkout here and a container over SSH
 * was called after whichever happened to be created first — and two workspaces that shared that
 * first environment were indistinguishable in the sidebar, which is the one place you pick
 * between them. A workspace *is* its environments; naming it after one of them hides the rest,
 * and naming it after one plus a tally hides them while admitting it.
 *
 * Joined with `Intl.ListFormat` rather than `", "`, because the separator is language: English
 * separates with a comma and Japanese with `、`, which carries no space around it.
 */
export function workspaceLabel(
  locations: LocationName[] | undefined,
  locale: string,
  fallback: string,
): string {
  const names = (locations ?? []).map(locationTargetLabel).filter(Boolean);
  if (names.length === 0) return fallback;
  try {
    return new Intl.ListFormat(locale, { style: "narrow", type: "conjunction" }).format(names);
  } catch {
    // A locale the platform does not know: the names still matter more than the separator.
    return names.join(", ");
  }
}
