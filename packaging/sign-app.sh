#!/usr/bin/env bash
# Sign the two Daisy artifacts with the local self-signed identity
# (packaging/create-signing-cert.sh), so the daemon has a STABLE code identity across rebuilds.
#
# There are two now, and only one of them matters for permissions. The daemon — not the desktop
# app — is the process that calls the macOS Accessibility API, because a session worker is a
# re-exec of it, and TCC lists whichever process exercises the permission. The daemon ships as
# "Daisy Computer Use.app", whose Info.plist carries the *same* CFBundleName ("Daisy") and
# identifier (com.ghovax.daisy) as the desktop app. Signed with the same persistent cert it
# satisfies the same designated requirement, so it folds into a single "Daisy" entry in
# Accessibility — no separate "daisy" row — and that grant survives rebuilds, where a fresh
# ad-hoc hash every build would not. Full Disk Access and the properly-iconed entry persist
# with it.
#
# The desktop app used to carry the daemon inside itself, which is why this script used to undo
# Tauri's symlink flattening: PyInstaller's codesignable layout cross-symlinks Contents/Frameworks
# and Contents/Resources, Tauri's resource copier dereferenced every one of those, and the app
# ballooned about fivefold. Nothing bundles the daemon now, so nothing dereferences anything, and
# that whole repair step is gone along with the coupling that made it necessary.
set -euo pipefail

IDENTITY="Daisy Local Codesign"

usage() {
  echo "usage: sign-app.sh <path-to-Daisy.app | path-to-Daisy Computer Use.app> [...]" >&2
  echo "  Sign either artifact, or both. The daemon takes --deep to catch its PyInstaller" >&2
  echo "  dylibs; the app has no nested code to descend into." >&2
  exit 1
}

[ "$#" -ge 1 ] || usage

for bundle in "$@"; do
  [ -d "$bundle" ] || { echo "not a bundle: $bundle" >&2; exit 1; }
  case "$(basename "$bundle")" in
    # The daemon: --deep so its own PyInstaller dylibs are covered. No hardened runtime, so
    # they still load; the entitlements it needs travel with it (packaging/Entitlements.plist).
    *"Computer Use.app")
      codesign --force --deep --entitlements "$(dirname "${BASH_SOURCE[0]}")/Entitlements.plist" \
        --sign "$IDENTITY" "$bundle"
      ;;
    # The desktop app: a plain Tauri shell with no nested code and no need to send Apple Events
    # or load unvalidated libraries — both of those were the bundled daemon's requirements.
    *)
      codesign --force --sign "$IDENTITY" "$bundle"
      ;;
  esac
  echo "signed $bundle"
  codesign -dv "$bundle" 2>&1 | grep -iE "Identifier=|Authority=" | sed 's/^/  /'
done
