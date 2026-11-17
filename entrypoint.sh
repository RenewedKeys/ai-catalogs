#!/bin/sh
# entrypoint.sh - dispatcher for the two services built from this image.
#
#   (no arguments) or "web"   start the web server (uvicorn)
#   anything else             exec it as given, e.g. `python -m app.worker`
#
# Every branch uses exec, so the service is PID 1 and receives SIGTERM directly.

set -e

if [ "$#" -gt 0 ] && [ "$1" != "web" ]; then
    exec "$@"
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
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "$UVICORN_WORKERS" --no-access-log
