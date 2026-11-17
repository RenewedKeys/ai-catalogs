#!/bin/sh
# Compose smoke test (PLAN.md, section 12). Needs Docker with the compose plugin.
#
#   web     PID 1 is uvicorn and /healthz answers 200
#   worker  PID 1 is the worker module and it publishes no port
#   stop    both exit with code 0 within the grace period
set -eu

cleanup() { docker compose down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

export PORT="${PORT:-18000}"
docker compose up -d --build

i=0
until curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 30 ]; then
        echo "web did not become healthy" >&2
        docker compose logs >&2
        exit 1
    fi
    sleep 1
done
echo "ok: /healthz"

web_cmd=$(docker compose exec -T web cat /proc/1/cmdline | tr '\0' ' ')
case "$web_cmd" in
    *uvicorn*) echo "ok: web PID 1 is uvicorn" ;;
    *) echo "web PID 1 is: $web_cmd" >&2; exit 1 ;;
esac

worker_cmd=$(docker compose exec -T worker cat /proc/1/cmdline | tr '\0' ' ')
case "$worker_cmd" in
    *app.worker*) echo "ok: worker PID 1 is app.worker" ;;
    *) echo "worker PID 1 is: $worker_cmd" >&2; exit 1 ;;
esac

if [ -n "$(docker compose port worker 8000 2>/dev/null || true)" ]; then
    echo "worker publishes a port" >&2
    exit 1
fi
echo "ok: worker publishes no port"

code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/configure")
[ "$code" = "404" ] || { echo "/configure answered $code with legacy off" >&2; exit 1; }
echo "ok: legacy routes are not mounted"

start=$(date +%s)
docker compose stop
elapsed=$(( $(date +%s) - start ))
[ "$elapsed" -le 30 ] || { echo "stop took ${elapsed}s" >&2; exit 1; }

for svc in web worker; do
    code=$(docker inspect -f '{{.State.ExitCode}}' "$(docker compose ps -a -q "$svc")")
    [ "$code" = "0" ] || { echo "$svc exited with $code" >&2; exit 1; }
done
echo "ok: both services stopped cleanly in ${elapsed}s"
