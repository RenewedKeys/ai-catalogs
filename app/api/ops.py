"""
Operations surface (PLAN.md section 10): ``/ops``, a sub-app with no CORS middleware.

    POST /ops/catalogs/{id}/refresh    queue a refresh   202 queued | 409 open job | 429 daily limit
    POST /ops/catalogs/{id}/dry-run    queue a dry run   (same)
    GET  /ops/status                   JSON; HTML when the request accepts text/html (or ?format=html)

Every path, including unknown ones, needs ``Authorization: Bearer <OPS_TOKEN>`` (compared in
constant time). With ``OPS_TOKEN`` unset every path answers 404, as if ``/ops`` did not exist.
The token is header-only; no cookies are involved, so there is no CSRF surface.

The routes only queue jobs (one open job per catalog, ``max_runs_per_day``); the worker runs
them. The status page escapes every value and shows only controlled data (see
``app.curation.status``).
"""

import hmac
import html
import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.curation.budget import BudgetLimits
from app.curation.status import collect_status
from app.store import db
from app.store.jobs import Enqueued, enqueue_job

_NOT_FOUND = {"detail": "Not Found"}


def _authorized(request: Request) -> bool:
    token = settings.OPS_TOKEN
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip().encode("utf-8"), token.encode("utf-8"))


def enqueue(catalog_id: str, kind: str, trigger: str = "ops") -> tuple[int, dict[str, Any]]:
    """Queue a job with run admission (shared by /ops and /configure). Returns (HTTP status, body)."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT state, definition_json FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)
        ).fetchone()
        if row is None or row["state"] != "enabled":
            return 404, {"status": "not_found", "detail": "no enabled catalog with this id"}
        try:
            limits = BudgetLimits.from_definition(json.loads(row["definition_json"]))
        except ValueError:
            return 409, {"status": "invalid_definition", "detail": "the stored definition is invalid"}
        status, job_id = enqueue_job(
            conn, catalog_id, kind=kind, trigger=trigger, max_runs_per_day=limits.max_runs_per_day
        )
        if status is Enqueued.QUEUED:
            return 202, {"status": "queued", "job_id": job_id, "kind": kind}
        if status is Enqueued.ALREADY_OPEN:
            job = conn.execute(
                "SELECT id, kind, state FROM jobs WHERE catalog_id = ? AND state IN ('pending', 'running')",
                (catalog_id,),
            ).fetchone()
            body = {"status": "already_open", "detail": "the catalog already has an open job"}
            if job is not None:
                body.update(job_id=job["id"], kind=job["kind"], state=job["state"])
            return 409, body
        if status is Enqueued.DAILY_LIMIT:
            return 429, {"status": "daily_limit", "detail": f"max_runs_per_day ({limits.max_runs_per_day}) reached"}
        return 404, {"status": "not_found", "detail": "no enabled catalog with this id"}


def _status() -> dict[str, Any]:
    with db.session() as conn:
        return collect_status(conn, db.now_ms())


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _ago(seconds: Any) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= 2 * size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _counts(d: Any) -> str:
    if not isinstance(d, dict) or not d:
        return ""
    return ", ".join(f"{_e(k)}: {_e(v)}" for k, v in d.items())


def render_html(status: dict[str, Any]) -> str:
    """A minimal, dependency-free page of the same data. Every value is escaped."""
    w, b, bk = status["worker"], status["budget"], status["backups"]
    worker = (
        f"lease held by {_e(w['holder'])} (generation {_e(w['generation'])}), heartbeat {_ago(w['heartbeat_age_s'])}"
        if w["lease_held"]
        else "<strong>lease not held</strong> (no worker running?)"
    )
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        "<meta name=viewport content='width=device-width, initial-scale=1'>",
        "<meta name=robots content=noindex><title>Catalog status</title>",
        "<style>body{font:14px/1.45 system-ui,sans-serif;margin:16px;max-width:1100px;color:#222;background:#fff}"
        "table{border-collapse:collapse;width:100%;margin:6px 0 18px}td,th{border-bottom:1px solid #ddd;"
        "padding:4px 6px;text-align:left;vertical-align:top}.err{color:#a40000}.muted{color:#666}"
        "@media(prefers-color-scheme:dark){body{background:#151515;color:#ddd}td,th{border-color:#333}"
        ".err{color:#ff7b72}.muted{color:#999}}</style></head><body>",
        f"<h1>Catalog status</h1><p class=muted>{_e(status['generated_at'])}</p>",
        f"<p>Worker: {worker}<br>Model calls today: {_e(b['model_calls_used'])} / {_e(b['model_calls_limit'])} "
        f"({_e(b['day'])} UTC)<br>Latest backup: {_e(bk['latest'] or 'none')} ({_ago(bk['latest_age_s'])})</p>",
    ]
    if status["definition_errors"]:
        parts.append("<h2>Definition files with errors</h2><ul>")
        parts += [f"<li class=err>{_e(e['file'])}: {_e(e['error'])}</li>" for e in status["definition_errors"]]
        parts.append("</ul>")
    for c in status["catalogs"]:
        snap, sched = c["active_snapshot"], c["schedule"]
        if sched is None:
            sched_text = "manual only"
        elif "error" in sched:
            sched_text = _e(sched["error"])
        else:
            sched_text = f"{_e(sched['cron'])} ({_e(sched['timezone'])}), next {_e(c['next_run_at'] or '-')}"
        parts.append(f"<h2>{_e(c['name'])} <span class=muted>{_e(c['catalog_id'])} · {_e(c['state'])}</span></h2>")
        parts.append(
            "<p>Active list: "
            + (f"{_e(snap['items'])} items, published {_ago(snap['age_s'])}" if snap else "none yet")
            + f"<br>Schedule: {sched_text}"
        )
        if c["open_job"]:
            j = c["open_job"]
            parts.append(
                f"<br>Open job {_e(j['id'])}: {_e(j['kind'])} ({_e(j['trigger'])}), {_e(j['state'])}, "
                f"attempt {_e(j['attempt'])}"
            )
        if c["last_error"]:
            parts.append(f"<br><span class=err>Definition error: {_e(c['last_error'])}</span>")
        parts.append("</p>")
        if c["recent_runs"]:
            parts.append(
                "<table><tr><th>Run</th><th>Kind</th><th>Status</th><th>Started</th><th>Calls</th>"
                "<th>Stage counts</th><th>Reject reasons</th></tr>"
            )
            for r in c["recent_runs"]:
                st = _e(r["status"])
                if r["error_category"]:
                    status_text = f"{_e(r['error_category'])} {_e(r['error_status'] or '')}".strip()
                    st += f" <span class=err>{status_text}</span>"
                parts.append(
                    f"<tr><td>{_e(r['id'])}</td><td>{_e(r['kind'])} ({_e(r['trigger'])})</td><td>{st}</td>"
                    f"<td>{_e(r['started_at'])}</td><td>{_e(r['model_calls'])}</td>"
                    f"<td>{_counts(r['stage_counts'])}</td><td>{_counts(r['reject_reasons'])}</td></tr>"
                )
            parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def create_ops_app() -> FastAPI:
    ops = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @ops.middleware("http")
    async def require_token(request: Request, call_next):
        # Before routing, so unknown paths and wrong methods leak nothing either.
        if not settings.OPS_TOKEN:
            return JSONResponse(_NOT_FOUND, status_code=404)
        if not _authorized(request):
            return JSONResponse(
                {"detail": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": 'Bearer realm="ops"'}
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @ops.post("/catalogs/{catalog_id}/refresh")
    async def refresh(catalog_id: str) -> Response:
        code, body = await run_in_threadpool(enqueue, catalog_id, "refresh")
        return JSONResponse(body, status_code=code)

    @ops.post("/catalogs/{catalog_id}/dry-run")
    async def dry_run(catalog_id: str) -> Response:
        code, body = await run_in_threadpool(enqueue, catalog_id, "dry_run")
        return JSONResponse(body, status_code=code)

    @ops.get("/status")
    async def status(request: Request) -> Response:
        data = await run_in_threadpool(_status)
        wants_html = request.query_params.get("format") == "html" or (
            request.query_params.get("format") != "json" and "text/html" in request.headers.get("accept", "")
        )
        if wants_html:
            return HTMLResponse(render_html(data))
        return JSONResponse(data)

    return ops
