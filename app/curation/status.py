"""
Operational status (PLAN.md section 10): the data behind ``GET /ops/status`` and CLI
``status``, and the worker health check (``status --check``).

Only our own controlled values are reported: ids, names, states, counts, times, error
categories and our own validation messages. Never prompts, definition content, provider text
or secrets.
"""

import json
import socket
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.config import settings
from app.curation.budget import ledger_day
from app.curation.schedule import ScheduleError, schedule_of
from app.store.backup import latest_backup
from app.store.jobs import LEASE_NAME, LEASE_TTL_MS

WORKER_PREFIX = "worker"
RECENT_RUNS = 5


def worker_owner_prefix(hostname: Optional[str] = None) -> str:
    """Lease owner prefix of a worker process on this host (``runner.default_owner("worker")``)."""
    return f"{WORKER_PREFIX}-{hostname or socket.gethostname()}-"


def iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_s(now: int, ms: Optional[int]) -> Optional[int]:
    return None if ms is None else max(0, (now - ms) // 1000)


def _json(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def worker_status(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    row = conn.execute("SELECT owner, generation, expires_at FROM lease WHERE name = ?", (LEASE_NAME,)).fetchone()
    owner = row["owner"] if row else None
    held = bool(row and owner and row["expires_at"] > now)
    renewed = row["expires_at"] - LEASE_TTL_MS if held else None
    return {
        "lease_held": held,
        "holder": (owner.split("-", 1)[0] if owner else None) if held else None,  # worker | cli
        "owner": owner if held else None,
        "generation": row["generation"] if row else 0,
        "last_heartbeat": iso(renewed),
        "heartbeat_age_s": _age_s(now, renewed),
    }


def worker_healthy(conn: sqlite3.Connection, now: int, hostname: Optional[str] = None) -> tuple[bool, str]:
    """
    The worker's Docker health check: a worker process on this host holds an unexpired lease
    (the heartbeat task renews it every 20 s, during runs too). A CLI run holding the lease does
    not count.
    """
    row = conn.execute("SELECT owner, expires_at FROM lease WHERE name = ?", (LEASE_NAME,)).fetchone()
    if row is None or not row["owner"] or row["expires_at"] <= now:
        return False, "no process holds the worker lease"
    if not row["owner"].startswith(worker_owner_prefix(hostname)):
        return False, "the lease is held by another process, not this host's worker"
    age = _age_s(now, row["expires_at"] - LEASE_TTL_MS)
    return True, f"worker holds the lease (last heartbeat {age}s ago)"


def _catalog(conn: sqlite3.Connection, row: sqlite3.Row, now: int, runs_limit: int = RECENT_RUNS) -> dict[str, Any]:
    cid = row["catalog_id"]
    try:
        schedule = schedule_of(_json(row["definition_json"]) or {})
        schedule_out = {"cron": schedule.cron, "timezone": schedule.timezone} if schedule else None
    except ScheduleError:
        schedule_out = {"error": "invalid schedule"}
    snap = conn.execute(
        "SELECT id, created_at, item_count FROM snapshots WHERE catalog_id = ? AND status = 'active'", (cid,)
    ).fetchone()
    nxt = conn.execute("SELECT next_run_at FROM schedule WHERE catalog_id = ?", (cid,)).fetchone()
    job = conn.execute(
        "SELECT id, kind, trigger, state, not_before, attempt FROM jobs "
        "WHERE catalog_id = ? AND state IN ('pending', 'running')",
        (cid,),
    ).fetchone()
    runs = conn.execute(
        """
        SELECT id, trigger, dry_run, status, started_at, finished_at, stage_counts_json, reject_reasons_json,
               model_calls, tokens_in, tokens_out, relaxed_window, repeats, error_category, error_status
        FROM runs WHERE catalog_id = ? ORDER BY id DESC LIMIT ?
        """,
        (cid, runs_limit),
    ).fetchall()
    return {
        "catalog_id": cid,
        "name": row["name"],
        "state": row["state"],
        "source": row["source"],
        "last_error": row["last_error"],
        "schedule": schedule_out,
        "next_run_at": iso(nxt["next_run_at"]) if nxt else None,
        "active_snapshot": (
            {
                "id": snap["id"],
                "created_at": iso(snap["created_at"]),
                "age_s": _age_s(now, snap["created_at"]),
                "items": snap["item_count"],
            }
            if snap
            else None
        ),
        "open_job": (
            {
                "id": job["id"],
                "kind": job["kind"],
                "trigger": job["trigger"],
                "state": job["state"],
                "not_before": iso(job["not_before"]),
                "attempt": job["attempt"],
            }
            if job
            else None
        ),
        "recent_runs": [
            {
                "id": r["id"],
                "kind": "dry_run" if r["dry_run"] else "refresh",
                "trigger": r["trigger"],
                "status": r["status"],
                "started_at": iso(r["started_at"]),
                "finished_at": iso(r["finished_at"]),
                "stage_counts": _json(r["stage_counts_json"]),
                "reject_reasons": _json(r["reject_reasons_json"]),
                "model_calls": r["model_calls"],
                "tokens_in": r["tokens_in"],
                "tokens_out": r["tokens_out"],
                "relaxed_window": r["relaxed_window"],
                "repeats": r["repeats"],
                "error_category": r["error_category"],
                "error_status": r["error_status"],
            }
            for r in runs
        ],
    }


def catalog_status(
    conn: sqlite3.Connection, catalog_id: str, now: int, runs_limit: int = RECENT_RUNS
) -> Optional[dict[str, Any]]:
    """One catalog's entry of ``collect_status`` (the /configure catalog page asks for more runs)."""
    row = conn.execute(
        "SELECT catalog_id, name, state, source, last_error, definition_json FROM catalog_defs WHERE catalog_id = ?",
        (catalog_id,),
    ).fetchone()
    return _catalog(conn, row, now, runs_limit) if row else None


def collect_status(conn: sqlite3.Connection, now: int, db_path: Optional[str] = None) -> dict[str, Any]:
    day = ledger_day(now)
    used = conn.execute("SELECT COUNT(*) FROM llm_ledger WHERE day = ?", (day,)).fetchone()[0]
    backup = latest_backup(db_path)
    rows = conn.execute(
        "SELECT catalog_id, name, state, source, last_error, definition_json FROM catalog_defs "
        "ORDER BY state = 'removed', position, catalog_id"
    ).fetchall()
    errors = conn.execute("SELECT catalog_id, file, error, at FROM definition_errors ORDER BY catalog_id").fetchall()
    return {
        "generated_at": iso(now),
        "worker": worker_status(conn, now),
        "budget": {"day": day, "model_calls_used": used, "model_calls_limit": settings.MAX_MODEL_CALLS_PER_DAY},
        "backups": {
            "latest": backup[1] if backup else None,
            "latest_at": iso(backup[0]) if backup else None,
            "latest_age_s": _age_s(now, backup[0]) if backup else None,
        },
        "definition_errors": [
            {"catalog_id": e["catalog_id"], "file": e["file"], "error": e["error"], "at": iso(e["at"])} for e in errors
        ],
        "catalogs": [_catalog(conn, r, now) for r in rows],
    }


def _ago(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def text_lines(status: dict[str, Any]) -> list[str]:
    """Plain-text rendering for CLI ``status``."""
    w, b = status["worker"], status["budget"]
    lines = [
        f"status at {status['generated_at']}",
        (
            f"worker: lease held by {w['holder']} (generation {w['generation']}), heartbeat {_ago(w['heartbeat_age_s'])}"
            if w["lease_held"]
            else "worker: lease not held (no worker running?)"
        ),
        f"budget: {b['model_calls_used']}/{b['model_calls_limit']} model calls today ({b['day']} UTC)",
        f"backups: latest {status['backups']['latest'] or 'none'} ({_ago(status['backups']['latest_age_s'])})",
    ]
    for e in status["definition_errors"]:
        lines.append(f"definition error: {e['file']}: {e['error']}")
    for c in status["catalogs"]:
        snap = c["active_snapshot"]
        snap_text = f"{snap['items']} items, {_ago(snap['age_s'])}" if snap else "no snapshot"
        sched = c["schedule"]
        sched_text = (
            "manual only"
            if sched is None
            else sched.get("error") or f"{sched['cron']} {sched['timezone']}, next {c['next_run_at'] or '-'}"
        )
        lines.append(f"{c['catalog_id']} [{c['state']}]: {snap_text}; schedule {sched_text}")
        if c["last_error"]:
            lines.append(f"  definition error: {c['last_error']}")
        if c["open_job"]:
            j = c["open_job"]
            lines.append(f"  open job {j['id']}: {j['kind']} ({j['trigger']}) {j['state']}, attempt {j['attempt']}")
        for r in c["recent_runs"][:3]:
            err = f" {r['error_category']}" if r["error_category"] else ""
            lines.append(f"  run {r['id']} {r['kind']} ({r['trigger']}) {r['status']}{err} at {r['started_at']}")
    return lines
