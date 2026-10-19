#!/usr/bin/env bash
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SOURCE/../.." && pwd)"
TARGET="${1:-/tmp/lemoncrow-review-demo}"

# Install into both the checkout and the local server before any Review capture.
# Provisioned demo runtimes may install the same plugin explicitly before this
# script runs so they do not restart an unrelated developer server.
if [[ "${LEMONCROW_DEMO_SKIP_PLUGIN_SETUP:-0}" != "1" ]]; then
    make -C "$REPO_ROOT" review-demo-plugin
fi
rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -a "$SOURCE/." "$TARGET/"

# Materialize baseline binary media before the baseline commit. The prepared
# change swaps these bytes after the commit, so Review can compare exact old/new
# GIF, video, audio, and PDF artifacts without requiring ffmpeg or other tools.
mkdir -p "$TARGET/media"
cp -a "$TARGET/.fixtures/media/before/." "$TARGET/media/"

npm install --prefix "$TARGET/frontend" --no-audit --no-fund

git -C "$TARGET" init -b main >/dev/null
git -C "$TARGET" config user.name "LemonCrow Demo"
git -C "$TARGET" config user.email "demo@lemoncrow.local"
git -C "$TARGET" add .
git -C "$TARGET" commit -m "baseline review demo" >/dev/null

"$TARGET/scripts/apply-demo-change.sh" --phase initial

# Remove only earlier demo-owned hook ledgers for this exact workspace before
# the server captures the new Change; otherwise Development would correctly
# import those historical sessions into the fresh Review.
uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --prune-only

LC_BIN="${LEMONCROW_DEMO_LC_BIN:-$HOME/.lemoncrow/bin/lc}"
if [[ -x "$LC_BIN" ]]; then
    LC=("$LC_BIN")
else
    LC=(uv run --project "$REPO_ROOT" lc)
fi
capture_log="$(mktemp)"
trap 'rm -f "$capture_log"' EXIT
if ! "${LC[@]}" review --repo-root "$TARGET" --new --track --no-open >"$capture_log" 2>&1; then
    cat "$capture_log" >&2
    exit 1
fi
current_json="$("${LC[@]}" review --repo-root "$TARGET" --current --json)"
review_id="$(printf '%s' "$current_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review_id"])')"
reader_url="$(printf '%s' "$current_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reader_url"])')"

# Finish the product change only after Snapshot 1 exists. The emitted agent
# messages are therefore captured against a real server-owned Review, and the
# next Snapshot can own them exactly rather than backdating them.
"$TARGET/scripts/apply-demo-change.sh" --phase final
uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --server-review "$review_id" --ledger-only

if ! "${LC[@]}" review --repo-root "$TARGET" --continue "$review_id" --track --no-open >"$capture_log" 2>&1; then
    cat "$capture_log" >&2
    exit 1
fi

# Snapshot 2 now owns the pre-existing hook output. Import/verify against the
# exact live Reader Review: sessions, annotations, Brief context, edit
# provenance, SessionEnd lifecycle, and test evidence all come from real stores.
uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --server-review "$review_id" --verify-only

cat <<EOF

Demo repository ready: $TARGET

Inspect setup:
  uv run lc review --repo-root $TARGET --setup

Open seeded Review:
  $reader_url
  ${LC[*]} review --repo-root $TARGET --open-review $review_id

The baseline commit is clean; the prepared product changes are intentionally uncommitted.

CSV table previews are enabled by the demo-installed csv-table plugin.
EOF
