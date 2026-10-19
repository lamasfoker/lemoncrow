#!/bin/sh
# Keep this formatter self-contained: release helpers also run without a checkout.
lc_ui_printf() (
    lc_ui_style=$1
    shift
    # Nested installers leave the summary to their caller; warnings stay visible.
    if [ "${LEMONCROW_INSTALL_NESTED:-0}" = 1 ] &&
        [ "${LEMONCROW_VERBOSE:-0}" != 1 ] && [ "${DRY_RUN:-0}" != 1 ]; then
        case "$lc_ui_style" in
            '1;31'|'1;33'|progress) ;;
            *) return 0 ;;
        esac
    fi
    lc_ui_force=${FORCE_COLOR:-${CLICOLOR_FORCE:-0}}
    if [ -z "${NO_COLOR:-}" ] && [ "${FORCE_COLOR:-}" != 0 ] &&
        { [ "$lc_ui_force" != 0 ] || { [ -t 1 ] && [ "${TERM:-}" != dumb ]; }; }; then
        lc_ui_format=$1
        shift
        case "$lc_ui_format" in
            '\n'*) printf '\033[2m│\033[0m\n'; lc_ui_format=${lc_ui_format#??} ;;
        esac
        # Rail, marker and text occupy the same columns in every message.
        case "$lc_ui_format" in '  '*) lc_ui_format=${lc_ui_format#  } ;; esac
        # Section diamonds replace the rail; child rows keep the rail.
        if [ "$lc_ui_style" != '1;36' ]; then
            printf '\033[2m│\033[0m  '
        fi
        case "$lc_ui_style" in
            '1;36')
                case "$lc_ui_format" in '◆ '*) lc_ui_format=${lc_ui_format#◆ } ;; esac
                printf '\033[38;2;155;117;217m◆  '
                printf "$lc_ui_format" "$@"
                lc_ui_status=$?
                printf '\033[0m'
                exit "$lc_ui_status"
                ;;
            '1;31')
                printf '\033[1;31m✗\033[0m  ' ;;
            '1;33')
                printf '\033[33m⚠\033[0m  ' ;;
            '1;32')
                case "$lc_ui_format" in '✓ '*) lc_ui_format=${lc_ui_format#✓ } ;; esac
                printf '\033[32m✓\033[0m  ' ;;
            progress) printf '\033[2m◇\033[0m  ' ;;
            command)
                printf '   %s \033[38;2;155;117;217m%s\033[0m%s\n' "$lc_ui_format" "$1" "${2:-}"
                exit $?
                ;;
            *) printf '   ' ;;
        esac
        printf "$lc_ui_format" "$@"
    else
        if [ "$lc_ui_style" = command ]; then
            printf '%s %s%s\n' "$1" "$2" "${3:-}"
        else
            printf "$@"
        fi
    fi
)

# Shared terminal messages for Make recipes and artifact builders.
# Source this file for lc_log, or execute it as: sh terminal.sh LEVEL MESSAGE.
lc_log() (
    lc_level=$1
    shift
    case "$lc_level" in
        step) lc_style='38;2;155;117;217'; lc_mark='◆' ;;
        success) lc_style='32'; lc_mark='✓' ;;
        warn) lc_style='33'; lc_mark='⚠' ;;
        error) lc_style='1;31'; lc_mark='✗' ;;
        hint) lc_style='2'; lc_mark='→' ;;
        *) lc_style='2'; lc_mark='◇' ;;
    esac
    lc_force=${FORCE_COLOR:-${CLICOLOR_FORCE:-0}}
    if [ -z "${NO_COLOR:-}" ] && [ "${FORCE_COLOR:-}" != 0 ] &&
        { [ "$lc_force" != 0 ] || { [ -t 1 ] && [ "${TERM:-}" != dumb ]; }; }; then
        case "$lc_level" in
            step) printf '\033[2m│\033[0m\n\033[%sm%s  %s\033[0m\n' "$lc_style" "$lc_mark" "$*" ;;
            *) printf '\033[2m│\033[0m  \033[%sm%s\033[0m  %s\n' "$lc_style" "$lc_mark" "$*" ;;
        esac
    else
        printf '%s  %s\n' "$lc_mark" "$*"
    fi
)

# Internal progress and artifact paths are available in verbose mode.
lc_verbose() {
    [ "${LEMONCROW_VERBOSE:-0}" != 1 ] || lc_log "$@"
}

# Capture routine build chatter; always replay failed output and clean up.
lc_run() (
    if [ "${LEMONCROW_VERBOSE:-0}" = 1 ]; then exec "$@"; fi
    lc_output=$(mktemp "${TMPDIR:-/tmp}/lemoncrow-command.XXXXXX") || exit 1
    trap 'rm -f "$lc_output"' 0
    if "$@" >"$lc_output" 2>&1; then
        exit 0
    else
        lc_status=$?
        lc_log error "Command failed: $1 (exit $lc_status)" >&2
        cat "$lc_output" >&2
        exit "$lc_status"
    fi
)
case "${0##*/}" in
    terminal.sh) lc_log "$@" ;;
esac
