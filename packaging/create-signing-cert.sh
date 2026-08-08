#!/usr/bin/env bash
# Create a persistent, self-signed code-signing identity in the login keychain, used to
# sign local LangMesh builds. A stable signing identity (vs. per-build ad-hoc) gives the
# bundled server a STABLE code identity, so the macOS Accessibility grant the user gives
# it survives rebuilds instead of being invalidated by a fresh ad-hoc hash each time.
#
# Idempotent: does nothing if the identity already exists. One-time setup; reversible by
# deleting "LangMesh Local Codesign" from Keychain Access.
set -euo pipefail

IDENTITY="LangMesh Local Codesign"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-certificate -c "$IDENTITY" "$KEYCHAIN" >/dev/null 2>&1; then
  echo "signing identity '$IDENTITY' already present"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/cert.conf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = LangMesh Local Codesign
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF

# Generate the key + self-signed code-signing certificate.
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$WORK/langmesh.key" -out "$WORK/langmesh.crt" -config "$WORK/cert.conf" 2>/dev/null

# Bundle with the system LibreSSL because macOS `security import` rejects OpenSSL 3.x's default MAC.
/usr/bin/openssl pkcs12 -export -inkey "$WORK/langmesh.key" -in "$WORK/langmesh.crt" \
  -out "$WORK/langmesh.p12" -name "$IDENTITY" -passout pass:langmesh 2>/dev/null

# Import into the login keychain and let codesign (and any tool, -A) use the key without
# an interactive keychain-access prompt.
security import "$WORK/langmesh.p12" -k "$KEYCHAIN" -P langmesh -T /usr/bin/codesign -A

echo "created signing identity '$IDENTITY'"
