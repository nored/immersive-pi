#!/bin/bash
# Build an encrypted A/B update bundle and publish it as a GitHub release.
#   ./release.sh [pi-vX.Y.Z]
#
# Requires output/immersive-rootfs.ext4 + output/immersive-boot.tar (from
# build.sh), secrets/update.key (the SAME 32 bytes baked into images), and an
# authenticated gh CLI. Bundle = [16B IV][AES-256-CBC ciphertext][32B HMAC].
set -euo pipefail

VERSION="${1:-$(cat VERSION 2>/dev/null || true)}"
[[ -n "$VERSION" ]] || { echo "usage: $0 <pi-vX.Y.Z> (or run ./build.sh first)"; exit 1; }
case "$VERSION" in pi-v*) ;; *) echo "version must be pi-vX.Y.Z"; exit 1 ;; esac

REPO="${REPO:-nored/immersive-releases}"
ROOTFS="output/immersive-rootfs.ext4"
BOOT="output/immersive-boot.tar"
KEYFILE="secrets/update.key"
WORK="output/release/${VERSION}"

[[ -f "$ROOTFS" ]] || { echo "missing $ROOTFS — run ./build.sh first"; exit 1; }
[[ -f "$BOOT" ]]   || { echo "missing $BOOT — run ./build.sh first"; exit 1; }
[[ -f "$KEYFILE" ]] || { echo "missing $KEYFILE — head -c 32 /dev/urandom > $KEYFILE"; exit 1; }

mkdir -p "$WORK"

echo "==> Bundling rootfs + boot..."
# --format ustar: busybox tar on the Pi can't read GNU sparse members by name.
tar --format ustar -C output -cf "$WORK/bundle.tar" immersive-rootfs.ext4 immersive-boot.tar

echo "==> Compressing (zstd -19)..."
zstd -q -19 -f "$WORK/bundle.tar" -o "$WORK/bundle.tar.zst"
rm "$WORK/bundle.tar"

MASTER_HEX=$(xxd -p -c 256 "$KEYFILE")
ENC_KEY_HEX=$(printf 'immersive-enc:%s' "$MASTER_HEX" | openssl dgst -sha256 -r | awk '{print $1}')
MAC_KEY_HEX=$(printf 'immersive-mac:%s' "$MASTER_HEX" | openssl dgst -sha256 -r | awk '{print $1}')

echo "==> Encrypting (AES-256-CBC) then HMAC-SHA256..."
IV_HEX=$(head -c 16 /dev/urandom | xxd -p)
openssl enc -aes-256-cbc -K "$ENC_KEY_HEX" -iv "$IV_HEX" \
    -in "$WORK/bundle.tar.zst" -out "$WORK/body.enc"
printf '%s' "$IV_HEX" | xxd -r -p > "$WORK/bundle.tar.zst.enc"
cat "$WORK/body.enc" >> "$WORK/bundle.tar.zst.enc"
openssl dgst -sha256 -mac HMAC -macopt hexkey:"$MAC_KEY_HEX" \
    -binary "$WORK/bundle.tar.zst.enc" >> "$WORK/bundle.tar.zst.enc"
rm -f "$WORK/body.enc" "$WORK/bundle.tar.zst"

ART="$WORK/bundle.tar.zst.enc"
SIZE=$(stat -c%s "$ART" 2>/dev/null || stat -f%z "$ART")
SHA=$(sha256sum "$ART" | awk '{print $1}')

cat > "$WORK/manifest.json" <<EOF
{
  "version": "$VERSION",
  "artifact": "bundle.tar.zst.enc",
  "size": $SIZE,
  "sha256": "$SHA",
  "encryption": "aes-256-cbc-then-hmac-sha256",
  "compression": "zstd",
  "contents": ["immersive-rootfs.ext4", "immersive-boot.tar"]
}
EOF
echo "==> Manifest:"; cat "$WORK/manifest.json"

echo "==> Publishing $REPO release $VERSION..."
gh release create "$VERSION" --repo "$REPO" \
    --title "immersive image $VERSION" \
    --notes "Encrypted A/B update bundle (rootfs + boot) for immersive-pi. SHA256: $SHA" \
    "$ART#bundle.tar.zst.enc" "$WORK/manifest.json#manifest.json" || \
gh release upload "$VERSION" --repo "$REPO" --clobber \
    "$ART#bundle.tar.zst.enc" "$WORK/manifest.json#manifest.json"

echo "==> Done. Nodes polling nightly pick up $VERSION."
