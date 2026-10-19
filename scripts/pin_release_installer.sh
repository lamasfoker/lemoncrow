#!/usr/bin/env bash
# Stamp a staged install.sh with its release tag and the SHA-256 of every asset
# it may run or install, so the published installer verifies helpers and product
# archives against hashes it carries itself instead of checksum sidecars served
# from the same origin as the artifacts.
#
# Usage: bash scripts/pin_release_installer.sh STAGE_DIR [EXTRA_ASSET_DIR...]
#   STAGE_DIR        staged release directory holding install.sh and its assets
#   EXTRA_ASSET_DIR  further directories of published *.tar.gz assets to pin
#   LEMONCROW_RELEASE_TAG  tag the assets are published under (optional; when
#                    unset the pins apply to whichever release base is used)
set -euo pipefail

STAGE="${1:-}"
[[ -n "$STAGE" && -f "$STAGE/install.sh" ]] || { echo "usage: pin_release_installer.sh STAGE_DIR [EXTRA_ASSET_DIR...]" >&2; exit 2; }
shift
TAG="${LEMONCROW_RELEASE_TAG:-}"
[[ "$TAG" != latest ]] || TAG=""
[[ -z "$TAG" || "$TAG" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "pin_release_installer.sh: invalid release tag: $TAG" >&2; exit 2; }

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'
    fi
}

pins=()
for asset in "$STAGE"/*.sh "$STAGE"/*.tar.gz; do
    [[ -f "$asset" && "${asset##*/}" != install.sh ]] || continue
    pins+=("${asset##*/}=$(sha256_file "$asset")")
done
for dir in "$@"; do
    for asset in "$dir"/*.tar.gz; do
        [[ -f "$asset" ]] || continue
        pins+=("${asset##*/}=$(sha256_file "$asset")")
    done
done
(( ${#pins[@]} )) || { echo "pin_release_installer.sh: no assets to pin in $STAGE" >&2; exit 1; }
for pin in "${pins[@]}"; do
    [[ "$pin" =~ ^[A-Za-z0-9._-]+=[0-9a-f]{64}$ ]] || { echo "pin_release_installer.sh: unexpected asset name: ${pin%%=*}" >&2; exit 1; }
done

installer="$STAGE/install.sh"
for anchor in '_PUBLIC_PINNED_TAG=""' '_PUBLIC_PINNED_SHA256S=""'; do
    [[ "$(grep -cxF "$anchor" "$installer")" == 1 ]] \
        || { echo "pin_release_installer.sh: pin anchor missing or already stamped: $anchor" >&2; exit 1; }
done
tmp="$(mktemp "$installer.XXXXXX")"
awk -v tag="$TAG" -v pins="${pins[*]}" '
    $0 == "_PUBLIC_PINNED_TAG=\"\"" { print "_PUBLIC_PINNED_TAG=\"" tag "\""; next }
    $0 == "_PUBLIC_PINNED_SHA256S=\"\"" { print "_PUBLIC_PINNED_SHA256S=\"" pins "\""; next }
    { print }
' "$installer" >"$tmp"
chmod 755 "$tmp"
mv -f "$tmp" "$installer"
echo "Pinned ${#pins[@]} release assets into $installer${TAG:+ for $TAG}"
