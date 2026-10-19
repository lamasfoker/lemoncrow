#!/usr/bin/env bash
# smoke_cursor.sh — Real end-to-end smoke test for LemonCrow ↔ Cursor.
#
# Proves, against the actual cursor-agent binary, that:
#   1. A workspace install writes the MCP config, hooks, and rules.
#   2. verify_cursor.sh passes for that workspace.
#   3. cursor-agent can LOAD the lemoncrow MCP server and enumerate its tools
#      (deterministic — no model call, only needs the CLI logged in).
#   4. (opt-in) A real headless `cursor-agent -p` run records an ATTRIBUTED
#      cursor savings row that `lc usage optimize` then reflects.
#
# Everything runs in a throwaway temp workspace — your real ~/.cursor and
# global config are never touched.
#
# Env:
#   LEMONCROW_CURSOR_SMOKE_RUN=1   also drive a real `cursor-agent -p` turn
#                                  (needs the CLI logged in; spends a few tokens)
#
# Exit: 0 pass, 1 fail, 0 + "SKIPPED" when cursor-agent is absent.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/python_bootstrap.sh"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }
ok()   { echo "  ✓ $*"; }

if ! command -v cursor-agent >/dev/null 2>&1; then
    echo "=== SKIPPED (cursor-agent CLI not found) ==="
    exit 0
fi

WS="$(mktemp -d "${TMPDIR:-/tmp}/lc-cursor-smoke.XXXXXX")"
cleanup() { rm -rf "$WS"; }
trap cleanup EXIT

git -C "$WS" init -q
cat > "$WS/app.py" <<'EOF'
def add(a, b):
    return a + b


def multiply(a, b):
    return a * b
EOF
git -C "$WS" add -A >/dev/null 2>&1
git -C "$WS" -c user.email=smoke@lemoncrow.dev -c user.name=smoke commit -qm init

echo "== 1. install (workspace: $WS) =="
echo "== 1. install (workspace: $WS, enforce=hard) =="
bash "$SCRIPT_DIR/install_cursor.sh" --workspace "$WS" --enforce hard >/dev/null 2>&1 || fail "install_cursor.sh failed"
# MCP is global-only (project mcp.json under key lemoncrow drops the live client).
python3 - "$HOME/.cursor/mcp.json" <<'PY' || fail "global MCP missing lemoncrow --host cursor"
import json, sys
d = json.loads(open(sys.argv[1]).read())
lc = d.get("mcpServers", {}).get("lemoncrow") or {}
args = lc.get("args") or []
assert isinstance(lc, dict) and "cursor" in args, lc
PY
[ -f "$WS/.cursor/hooks/session_start.py" ] || fail "sessionStart hook not staged"
[ -f "$WS/.cursor/hooks/stop.py" ]      || fail "stop hook not staged"
[ -f "$WS/.cursor/hooks.json" ]         || fail "hooks.json not written"
grep -q 'LEMONCROW_CURSOR_ENFORCE=hard' "$WS/.cursor/hooks.json" || fail "hooks missing enforce=hard"
compgen -G "$WS/.cursor/rules/lemoncrow*.mdc" >/dev/null || fail "rules not installed"
ok "global MCP + workspace hooks/rules (enforce=hard)"
echo "== 2. verify_cursor.sh --workspace =="
bash "$SCRIPT_DIR/verify_cursor.sh" --workspace "$WS" || fail "verify_cursor.sh reported failures"
ok "verify passed"

echo "== 3. cursor-agent loads the lemoncrow MCP server =="
TOOLS="$(cd "$WS" && timeout 90 cursor-agent mcp list-tools lemoncrow 2>&1 || true)"
echo "$TOOLS" | sed 's/^/    /'
echo "$TOOLS" | grep -qiE '^Tools for lemoncrow|(^| )- (code_search|read|edit|bash)' \
    || fail "cursor-agent could not enumerate lemoncrow tools (is the CLI logged in?)"
ok "cursor-agent enumerated lemoncrow tools"

if [ "${LEMONCROW_CURSOR_SMOKE_RUN:-0}" = "1" ]; then
    echo "== 4. real cursor-agent -p turn + savings attribution =="
    BEFORE=$(date +%s)
    ( cd "$WS" && timeout 240 cursor-agent -p --force --output-format text \
        "Use the code_search tool to find the add function, then read app.py. One-line summary. Do not edit anything." ) \
        >/dev/null 2>&1 || fail "cursor-agent -p run failed"
    ROW="$(find "${HOME}/.lemoncrow/sessions" -path '*/cursor/*' -name savings.jsonl -newermt "@$BEFORE" 2>/dev/null | head -1)"
    [ -n "$ROW" ] || fail "no cursor savings.jsonl written by the run"
    grep -q '"unattributed": true' "$ROW" && fail "savings row landed in the unattributed quarantine ledger"
    ok "attributed cursor savings row: $ROW"
    echo "    $(cat "$ROW")"
else
    echo "== 4. skipped (set LEMONCROW_CURSOR_SMOKE_RUN=1 to drive a real turn) =="
fi

echo "=== PASS: Cursor smoke test ==="
