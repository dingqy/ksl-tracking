#!/bin/sh
# Wrapper for cron / Synology Task Scheduler: load config.env, then run.
# POSIX sh only -- works with busybox on Synology and QNAP.
#
# Values already present in the environment WIN over config.env, so you can
# override anything for a one-off run:   KSL_MODE=any ./run.sh --once
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -f "$DIR/config.env" ]; then
    # Parsed line by line rather than `. config.env` so that (a) the file
    # cannot execute shell code, (b) an existing env var is not clobbered,
    # and (c) CRLF line endings from editing on Windows are tolerated.
    while IFS= read -r line || [ -n "$line" ]; do
        line=$(printf '%s' "$line" | tr -d '\r')
        case "$line" in ''|'#'*) continue ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key=${line%%=*}
        case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        eval "already=\${$key+set}" || already=
        [ "${already:-}" = set ] && continue
        export "$key=${line#*=}"
    done < "$DIR/config.env"
fi

[ $# -eq 0 ] && set -- --once
exec "${PYTHON:-python3}" "$DIR/ksl_watch.py" "$@"
