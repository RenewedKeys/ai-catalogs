#!/bin/sh
# Compose smoke test (PLAN.md, section 12). Needs Docker with the compose plugin.
#
#   web     PID 1 is uvicorn and /healthz answers 200
#   worker  PID 1 is the worker module and it publishes no port
#   store   the web workers create the database on the volume (racing on first start), the
#           curated manifest is empty, a fixture loaded with the CLI is served whole at
#           offset 0 and empty at skip=100, and it survives a container restart
#   stop    both stop within the grace period (worker exits 0; web exits 0 or 143)
#
# Isolated from any real deployment in this directory: it runs under its own Compose
# project name (so `down -v` only removes this run's containers and volume), builds its own
# image tag, and loads an empty env file instead of .env, so no real credentials are used.
set -eu

PROJECT="ai-catalogs-smoke-$$"
EMPTY_ENV=$(mktemp)
export ENV_FILE="$EMPTY_ENV"
export IMAGE="ai-catalogs:smoke"
export PORT="${SMOKE_PORT:-18000}"

dc() { docker compose -p "$PROJECT" --env-file "$EMPTY_ENV" "$@"; }

cleanup() {
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$EMPTY_ENV"
}
trap cleanup EXIT

dc up -d --build

i=0
until curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 30 ]; then
        echo "web did not become healthy" >&2
        dc logs >&2
        exit 1
    fi
    sleep 1
done
echo "ok: /healthz"

web_cmd=$(dc exec -T web cat /proc/1/cmdline | tr '\0' ' ')
case "$web_cmd" in
    *uvicorn*) echo "ok: web PID 1 is uvicorn" ;;
    *) echo "web PID 1 is: $web_cmd" >&2; exit 1 ;;
esac

worker_cmd=$(dc exec -T worker cat /proc/1/cmdline | tr '\0' ' ')
case "$worker_cmd" in
    *app.worker*) echo "ok: worker PID 1 is app.worker" ;;
    *) echo "worker PID 1 is: $worker_cmd" >&2; exit 1 ;;
esac

# `docker port` lists a container's published ports and prints nothing when there are none.
# (`docker compose port` is no use here: it prints ":0" for an unpublished port.)
worker_ports=$(docker port "$(dc ps -q worker)")
if [ -n "$worker_ports" ]; then
    echo "worker publishes a port: $worker_ports" >&2
    exit 1
fi
echo "ok: worker publishes no port"

code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/configure")
[ "$code" = "404" ] || { echo "/configure answered $code with legacy off" >&2; exit 1; }
echo "ok: legacy routes are not mounted"

base="http://localhost:$PORT/c/public"
json_get() { curl -fsS "$1" | python3 -c "import json, sys; print($2)"; }

n=$(json_get "$base/manifest.json" 'len(json.load(sys.stdin)["catalogs"])')
[ "$n" = "0" ] || { echo "empty database lists $n catalogs" >&2; exit 1; }
echo "ok: empty manifest on a fresh volume"

dc exec -T web python -m app.curation load-fixture fixtures/test_catalog_100.json

check_catalog() {
    ids=$(json_get "$base/manifest.json" '",".join(c["id"] for c in json.load(sys.stdin)["catalogs"])')
    [ "$ids" = "test_catalog" ] || { echo "manifest lists: $ids" >&2; exit 1; }
    n=$(json_get "$base/catalog/movie/test_catalog.json" 'len(json.load(sys.stdin)["metas"])')
    [ "$n" = "100" ] || { echo "offset 0 returned $n items" >&2; exit 1; }
    n=$(json_get "$base/catalog/movie/test_catalog/skip=100.json" 'len(json.load(sys.stdin)["metas"])')
    [ "$n" = "0" ] || { echo "skip=100 returned $n items" >&2; exit 1; }
}
check_catalog
echo "ok: fixture served whole at offset 0, empty at skip=100"

owner=$(dc exec -T web stat -c %U /data/ai-catalogs.sqlite)
[ "$owner" = "appuser" ] || { echo "database owned by $owner" >&2; exit 1; }

dc restart web
i=0
until curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -le 30 ] || { echo "web did not come back" >&2; exit 1; }; sleep 1
done
check_catalog
echo "ok: database on the volume, owned by appuser, survives a restart"

start=$(date +%s)
dc stop
elapsed=$(( $(date +%s) - start ))
[ "$elapsed" -le 30 ] || { echo "stop took ${elapsed}s" >&2; exit 1; }

exit_code() { docker inspect -f '{{.State.ExitCode}}' "$(dc ps -a -q "$1")"; }

code=$(exit_code worker)
[ "$code" = "0" ] || { echo "worker exited with $code" >&2; exit 1; }

# uvicorn shuts down gracefully and then, with a single worker process, re-raises SIGTERM,
# so 143 is also a clean stop. 137 would mean it was killed after the grace period.
code=$(exit_code web)
case "$code" in
    0|143) ;;
    *) echo "web exited with $code" >&2; exit 1 ;;
esac
echo "ok: both services stopped cleanly in ${elapsed}s"
