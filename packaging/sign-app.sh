#!/usr/bin/env bash
# Sign a built Daisy.app with the local self-signed identity (packaging/create-signing-cert.sh)
# so its bundled computer-use server has a STABLE code identity across rebuilds.
#
# The server — not the app — is the process that calls the macOS Accessibility API, and TCC
# lists whichever process exercises the permission. The server ships as a nested helper bundle,
# "Daisy Computer Use.app", whose Info.plist carries the *same* CFBundleName ("Daisy") and
# identifier (com.ghovax.daisy) as the desktop app. Signed with the same persistent cert, it
# satisfies the same designated requirement, so it folds into the app's single "Daisy"
# Accessibility entry — no separate "daisy-server" — and that grant stays valid across rebuilds
# (vs. a fresh ad-hoc hash every time). Full Disk Access and the properly-iconed entry persist
# with it too.
set -euo pipefail

APP="${1:?usage: sign-app.sh <path-to-Daisy.app>}"
IDENTITY="Daisy Local Codesign"
HELPER="$APP/Contents/Resources/server-bin/Daisy Computer Use.app"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Undo Tauri's symlink flattening. PyInstaller lays the frozen server out as a codesignable macOS
# bundle: real Mach-Os in Contents/Frameworks, real Python in Contents/Resources, and each side
# symlinks the other so the import root sees one merged tree — so `du` on the pristine bundle is
# ~380 MB. Tauri's resource copier dereferences those symlinks when it bundles the helper, turning
# every one into a full second copy and ballooning the app to ~1.8 GB. `ditto` preserves symlinks,
# so re-copying the pristine helper from server-bin (untouched by Tauri) restores the ~380 MB tree
# before we sign it in place.
pristine="$repo_root/web/src-tauri/server-bin/Daisy Computer Use.app"
if [ -d "$pristine" ] && [ -d "$HELPER" ]; then
  echo "restoring symlink-preserving helper (undo Tauri deref)"
  rm -rf "$HELPER"
  ditto "$pristine" "$HELPER"
fi

# Sign inside-out: the nested helper first, then the outer app. `--deep` on the app alone does
# NOT descend into a bundle nested under Contents/Resources/ (it only follows Frameworks/ and the
# standard nested-code locations), so signing just the app leaves the helper — the very process
# TCC tracks for Accessibility — ad-hoc, which is exactly the fresh-hash-every-build problem this
# script exists to prevent. So sign the helper explicitly (`--deep`, to catch its own PyInstaller
# dylibs), then re-seal the app so its signature covers the helper's new hash. Each bundle takes
# its identifier from its own Info.plist (both com.ghovax.daisy); no hardened runtime, so the
# helper's dylibs still load.
codesign --force --deep --sign "$IDENTITY" "$HELPER"
codesign --force --sign "$IDENTITY" "$APP"

echo "signed $APP"
codesign -dv "$HELPER" 2>&1 | grep -iE "Identifier=|Authority=" | sed 's/^/  helper: /'
codesign -dv "$APP" 2>&1 | grep -iE "Identifier=|Authority=" | sed 's/^/  app:    /'
