#!/usr/bin/env bash
# Remove only LemonCrow's extension and registration from Pi's native settings.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --workspace)
            [[ $# -ge 2 ]] || { echo "Missing value for --workspace" >&2; exit 1; }
            shift ;; # Pi's integration is global in every install scope.
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done
if $DRY_RUN; then
    echo "[dry-run] Remove LemonCrow from ${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/settings.json"
    exit 0
fi
if [[ -f "$SCRIPT_DIR/lib/pi_integration.py" ]]; then
    python3 "$SCRIPT_DIR/lib/pi_integration.py" --remove
else
    python3 "$SCRIPT_DIR/../client/src/lemoncrow_client/pi_integration.py" --remove
fi
