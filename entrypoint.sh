#!/bin/sh
# entrypoint.sh - dispatcher for the two services built from this image.
#
#   (no arguments) or "web"   start the web server (uvicorn)
#   anything else             exec it as given, e.g. `python -m app.worker`
#
# Both run as PUID:PGID (see below).
#
# Every branch uses exec, so the service is PID 1 and receives SIGTERM directly.

set -e

# Run as PUID:PGID (default 1000:1000), like linuxserver images. Started as root (the default),
# first make sure the data directories exist and belong to that user, so a bind mount Docker
# created as root just works. Started as another user (compose `user:`), run as that user.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
case "$PUID$PGID" in
    *[!0-9]*) echo "PUID and PGID must be numbers (got PUID=$PUID PGID=$PGID)" >&2; exit 2 ;;
esac
export PUID PGID

RUN_AS=""
if [ "$(id -u)" = "0" ]; then
    DB_DIR=$(dirname "${DATABASE_PATH:-/data/ai-catalogs.sqlite}")
    for dir in /data "$DB_DIR" "${BACKUP_DIR:-/data/backups}"; do
        mkdir -p "$dir"
        find "$dir" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} +
    done
    if [ "$PUID" != "0" ]; then
        RUN_AS="su-exec $PUID:$PGID"
    fi
fi

if [ "$#" -gt 0 ] && [ "$1" != "web" ]; then
    exec $RUN_AS "$@"
fi

# Number of uvicorn workers: UVICORN_WORKERS, or one per CPU core (1..8) when unset or 0.
if [ -z "$UVICORN_WORKERS" ] || [ "$UVICORN_WORKERS" = "0" ]; then
    CPU_CORES=$(nproc)
    UVICORN_WORKERS=$CPU_CORES
    if [ "$UVICORN_WORKERS" -lt 1 ]; then
        UVICORN_WORKERS=1
    elif [ "$UVICORN_WORKERS" -gt 8 ]; then
        UVICORN_WORKERS=8
    fi
    echo "Auto-configured $UVICORN_WORKERS worker(s) based on $CPU_CORES CPU core(s)"
else
    echo "Using $UVICORN_WORKERS worker(s) as specified in UVICORN_WORKERS environment variable"
fi

# --no-access-log: request paths can carry the addon access token. The app logs requests
# itself with those path segments masked.
exec $RUN_AS uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "$UVICORN_WORKERS" --no-access-log
