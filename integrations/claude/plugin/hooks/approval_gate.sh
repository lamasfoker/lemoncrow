#!/usr/bin/env bash
# approval_gate.sh — Claude Code PreToolUse entry for LemonCrow's risky-action
# approval gate (Review Phase 12 item 14).
#
# Runs hooks/approval_gate.py through _run_hook.sh. When that cannot run at all
# (no usable python3, a broken interpreter, a crash before the gate answers)
# while any review.approval.<class> setting is on, the tool call is BLOCKED:
# exit 2 with the reason on stderr (fail closed). With no class configured a
# broken gate never blocks anything.
#
# "Configured" is read from plugin_settings.json itself, without python, not
# from a marker file: the settings file stays the one source of truth, so hand
# edits, imports and every settings writer are seen with nothing to keep in
# sync. The read is conservative: any review.approval.<name> key whose value is
# not plainly false counts as on, and a file that cannot be read or is not a
# JSON object counts as configured.

set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

approval_configured() {
    command -v grep >/dev/null 2>&1 && command -v tr >/dev/null 2>&1 || return 0
    local root="${LEMONCROW_ROOT:-}"
    # shellcheck disable=SC2088  # a literal leading ~ in LEMONCROW_ROOT is expanded here
    if [[ -z "${root//[[:space:]]/}" ]]; then
        root="${HOME:-}/.lemoncrow"
    elif [[ "${root}" == "~" || "${root}" == "~/"* ]]; then
        root="${HOME:-}${root:1}"
    fi
    local file="${root}/plugin_settings.json"
    [[ -e "${file}" ]] || return 1
    local flat
    flat="$(tr -d ' \t\r\n' <"${file}" 2>/dev/null)" || return 0
    [[ "${flat}" == "{"*"}" ]] || return 0
    local entry value
    while IFS= read -r entry; do
        [[ "${entry}" == '"review.approval.wait_seconds":'* ]] && continue
        value="${entry#*\":}"
        case "$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')" in
            false | '"false"' | 0 | '"0"' | '"no"' | '"off"' | '""' | null) ;;
            *) return 0 ;;
        esac
    done < <(printf '%s' "${flat}" | grep -oE '"review\.approval\.[^"]*":[^,}]*')
    return 1
}

bash "${here}/_run_hook.sh" "${here}/approval_gate.py"
status=$?
if [[ ${status} -eq 0 ]]; then
    exit 0
fi
if approval_configured; then
    echo "LemonCrow's approval gate could not run (exit ${status}); risky-action approval is configured, so this tool call is blocked (fail closed). Reinstall LemonCrow so python3 can import it, or turn off the review.approval settings." >&2
    exit 2
fi
# Nothing configured: never block (exit 2 would); keep the failure visible.
exit $((status == 2 ? 1 : status))
