#!/usr/bin/env bash
# Stage the published release assets: the single source of the asset list used by
# `make release-stage` and .github/public-workflows/release.yml.
#
# Usage: bash scripts/stage_release.sh OUT_DIR [DIST_DIR [PIN_DIR...]]
#   OUT_DIR   directory to (re)create and fill with the assets
#   DIST_DIR  directory holding the built product tarballs (default: <repo>/dist)
#   PIN_DIR   further published *.tar.gz directories whose hashes the staged
#             install.sh pins (e.g. the platform binaries)
#
# The staged install.sh is stamped with the SHA-256 of every staged asset
# (scripts/pin_release_installer.sh; LEMONCROW_RELEASE_TAG names the tag).
#
# Any missing file is a hard failure: a partial release must never be staged.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
source "$ROOT/scripts/lib/terminal.sh"
OUT_DIR="${1:-}"
DIST_DIR="${2:-$ROOT/dist}"
[[ -n "$OUT_DIR" ]] || { echo "usage: stage_release.sh OUT_DIR [DIST_DIR]" >&2; exit 2; }

INSTALLER_SCRIPTS=(
    install hosted runtime review_engine engine_install local_server
    local_review local_runtime uninstall uninstall_product uninstall_review
    uninstall_runtime sessions
)
PRODUCT_ARCHIVES=(lemoncrow-review-client lemoncrow-review-engine lemoncrow-runtime-client)

missing=()
for name in "${INSTALLER_SCRIPTS[@]}"; do
    [[ -f "$ROOT/scripts/$name.sh" ]] || missing+=("$ROOT/scripts/$name.sh")
done
for name in "${PRODUCT_ARCHIVES[@]}"; do
    for suffix in .tar.gz .tar.gz.sha256; do
        [[ -s "$DIST_DIR/$name$suffix" ]] || missing+=("$DIST_DIR/$name$suffix")
    done
done
if (( ${#missing[@]} )); then
    printf 'stage_release.sh: missing release asset: %s\n' "${missing[@]}" >&2
    exit 1
fi

# Only replace a directory containing this stage's own regular files.
# A typo such as OUT_DIR=dist or scripts must never recursively erase sources.
if [[ -e "$OUT_DIR" || -L "$OUT_DIR" ]]; then
    [[ -d "$OUT_DIR" && ! -L "$OUT_DIR" ]] || { echo "stage_release.sh: output must be a real directory" >&2; exit 2; }
    for existing in "$OUT_DIR"/* "$OUT_DIR"/.[!.]* "$OUT_DIR"/..?*; do
        [[ -e "$existing" || -L "$existing" ]] || continue
        allowed=0
        for name in "${INSTALLER_SCRIPTS[@]}"; do
            [[ "$existing" != "$OUT_DIR/$name.sh" ]] || allowed=1
        done
        for name in "${PRODUCT_ARCHIVES[@]}"; do
            for suffix in .tar.gz .tar.gz.sha256; do
                [[ "$existing" != "$OUT_DIR/$name$suffix" ]] || allowed=1
            done
        done
        if [[ "$allowed" != "1" || ! -f "$existing" || -L "$existing" ]]; then
            echo "stage_release.sh: refusing to replace unrelated output: $existing" >&2
            exit 2
        fi
    done
fi
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
for name in "${INSTALLER_SCRIPTS[@]}"; do
    cp "$ROOT/scripts/$name.sh" "$OUT_DIR/$name.sh"
done
for name in "${PRODUCT_ARCHIVES[@]}"; do
    cp "$DIST_DIR/$name.tar.gz" "$DIST_DIR/$name.tar.gz.sha256" "$OUT_DIR/"
done
bash "$ROOT/scripts/pin_release_installer.sh" "$OUT_DIR" "${@:3}" >/dev/null
lc_verbose info "Staged assets: $OUT_DIR"
