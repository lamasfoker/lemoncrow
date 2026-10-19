#!/usr/bin/env bash
# verify_cursor.sh — Verify LemonCrow Cursor (IDE + cursor-agent CLI) install
#
# Checks:
#   1. Cursor present (cursor / cursor-agent CLI, or ~/.cursor) — else graceful skip
#   2. lc on PATH
#   3. MCP config (global ~/.cursor/mcp.json or workspace .cursor/mcp.json)
#      contains the lemoncrow server entry with args including --host cursor
#   4. hooks.json wires the sessionStart + stop hooks to our staged scripts
#   5. Workspace mode: .cursor/rules/lemoncrow*.mdc present
#   6. cursor-agent CLI (if installed): the lemoncrow MCP server is listed
#
# Options:
#   --workspace DIR  Verify project-local workspace config instead of global
#
# Exits 0 if all checks pass (or Cursor not found — graceful skip); 1 on failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/python_bootstrap.sh"

WORKSPACE=""
WORKSPACE_SET=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace)
            if [ $# -lt 2 ]; then echo "Missing value for --workspace" >&2; exit 1; fi
            WORKSPACE="$2"; WORKSPACE_SET=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# The server registration is global in every scope (see install_cursor.sh) --
# hooks and rules are what --workspace scopes.
MCP_FILE="${HOME}/.cursor/mcp.json"
HOOKS_FILE="${HOME}/.cursor/hooks.json"
RULES_DIR=""
if $WORKSPACE_SET; then
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
    HOOKS_FILE="${WORKSPACE}/.cursor/hooks.json"
    RULES_DIR="${WORKSPACE}/.cursor/rules"
fi

FAIL=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; FAIL=1; }
skip() { echo "SKIP: $*"; }

echo "=== LemonCrow Cursor verification ==="

if ! command -v cursor >/dev/null 2>&1 && ! command -v cursor-agent >/dev/null 2>&1 \
   && [ ! -d "${HOME}/.cursor" ] && ! $WORKSPACE_SET; then
    skip "Cursor not found (no 'cursor', 'cursor-agent', or ~/.cursor) — install from https://cursor.com"
    echo "=== SKIPPED (Cursor not detected) ==="
    exit 0
fi
command -v cursor-agent >/dev/null 2>&1 && pass "cursor-agent CLI found: $(cursor-agent --version 2>/dev/null | head -1)"
command -v cursor >/dev/null 2>&1 && pass "cursor IDE launcher found"

if command -v lc >/dev/null 2>&1; then
    pass "lc is available on PATH"
else
    fail "lc NOT found on PATH — run: make install"
fi

# --- MCP config -------------------------------------------------------------
if [ -f "$MCP_FILE" ]; then
    HAS=$(python3 - "$MCP_FILE" <<'PYEOF' 2>/dev/null || echo error
import json, sys
try:
    d = json.loads(open(sys.argv[1]).read() or "{}")
except Exception:
    print("parse-error"); sys.exit()
lc = d.get("mcpServers", {}).get("lemoncrow", {})
args = lc.get("args", []) if isinstance(lc, dict) else []
print("yes" if isinstance(lc, dict) and "cursor" in args else ("no-host" if lc else "no"))
PYEOF
)
    case "$HAS" in
        yes) pass "MCP config has lemoncrow server with --host cursor ($MCP_FILE)" ;;
        no-host) fail "MCP lemoncrow entry present but missing '--host cursor' args ($MCP_FILE)" ;;
        parse-error) fail "MCP config parse error: $MCP_FILE" ;;
        *) fail "MCP config missing lemoncrow entry: $MCP_FILE — run: scripts/install_cursor.sh" ;;
    esac
else
    fail "MCP config not found: $MCP_FILE — run: scripts/install_cursor.sh"
fi

# --- hooks ------------------------------------------------------------------
if [ -f "$HOOKS_FILE" ]; then
    HK=$(python3 - "$HOOKS_FILE" <<'PYEOF' 2>/dev/null || echo error
import json, sys
try:
    d = json.loads(open(sys.argv[1]).read() or "{}")
except Exception:
    print("parse-error"); sys.exit()
hooks = d.get("hooks", {}) if isinstance(d, dict) else {}
def has(evt, name):
    return any(isinstance(e, dict) and name in str(e.get("command", "")) for e in hooks.get(evt, []))
print("yes" if has("sessionStart", "session_start.py") and has("stop", "stop.py") else "no")
PYEOF
)
    case "$HK" in
        yes) pass "hooks.json wires sessionStart + stop ($HOOKS_FILE)" ;;
        parse-error) fail "hooks.json parse error: $HOOKS_FILE" ;;
        *) fail "hooks.json missing sessionStart/stop — run: scripts/install_cursor.sh" ;;
    esac
else
    fail "hooks.json not found: $HOOKS_FILE — run: scripts/install_cursor.sh"
fi

# --- rules (workspace only) -------------------------------------------------
if $WORKSPACE_SET; then
    if compgen -G "${RULES_DIR}/lemoncrow*.mdc" >/dev/null; then
        pass "Cursor rules installed under $RULES_DIR"
    else
        fail "Cursor rules missing under $RULES_DIR"
    fi
fi

# --- cursor-agent CLI: server listed ----------------------------------------
# Soft check: enumerating requires the CLI to be logged in and able to reach
# the server; a miss here is a warning, not a hard fail.
if command -v cursor-agent >/dev/null 2>&1; then
    LIST_DIR="${PWD}"
    $WORKSPACE_SET && LIST_DIR="${WORKSPACE}"
    if ( cd "$LIST_DIR" && cursor-agent mcp list 2>/dev/null ) | grep -q "^lemoncrow:"; then
        pass "cursor-agent lists the lemoncrow MCP server"
    else
        echo "WARN: cursor-agent did not list lemoncrow (approve once with: cursor-agent mcp enable lemoncrow)" >&2
    fi
fi

if [ "$FAIL" -ne 0 ]; then
    echo "=== FAIL: one or more Cursor checks failed ==="
    exit 1
fi
echo "=== PASS: all Cursor checks passed ==="
