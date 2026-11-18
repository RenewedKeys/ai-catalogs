"""
Lease, jobs and the fenced commit (PLAN.md section 4).

One process at a time may run refreshes: the holder of the ``worker`` lease. Every write a
lease holder makes on behalf of a job is *fenced*: it runs in one ``BEGIN IMMEDIATE``
transaction that first re-checks that this process still holds the lease under the same
generation, and only touches the job ``WHERE id = ? AND claim_gen = ? AND state =
'running'``. A process that lost the lease (paused, partitioned, slow) therefore cannot
write anything for a job that another process has since recovered or reclaimed; it gets
``LostLease`` and must stop.

Who calls what:
    acquire_lease / renew_lease / release_lease   the runner and its heartbeat task
    enqueue_job / admit_job                       scheduler, webhook, CLI (no lease needed)
    claim_job, heartbeat, finish_*, commit_run    the lease holder only
    recover_abandoned                             the lease holder, at start and every minute

This module only provides the primitives. ``app.curation.runner`` drives them (heartbeat
task, cancellation, wall-clock cap, recovery at start).
"""

import os
import signal
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from app.store.catalogs import SnapshotItem, publish_snapshot
from app.store.db import now_ms, transaction

LEASE_NAME = "worker"
LEASE_TTL_MS = 90_000
HEARTBEAT_STALE_MS = 5 * 60_000
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MS = (5 * 60_000, 15 * 60_000, 45 * 60_000)
DAY_MS = 24 * 3600_000


class LostLease(RuntimeError):
    """This process no longer holds the lease (or the job was reclaimed). Stop; write nothing."""


@dataclass(frozen=True)
class Lease:
    owner: str
    generation: int


@dataclass(frozen=True)
class Claim:
    job_id: int
    run_id: int
    catalog_id: str
    kind: str
    config_rev: str
    epoch: int
    claim_gen: int
    attempt: int


class Enqueued(Enum):
    QUEUED = "queued"
    ALREADY_OPEN = "already_open"  # one open job per catalog
    NOT_ENABLED = "not_enabled"
    DAILY_LIMIT = "daily_limit"


class Outcome(Enum):
    PUBLISHED = "published"
    DRY_RUN_OK = "dry_run_ok"
    SUPERSEDED = "superseded"


def _failpoint(name: str) -> None:
    """Crash-test hook: SIGKILL this process at a named point when the env var asks for it."""
    if os.environ.get("AI_CATALOGS_TEST_FAILPOINT") == name:
        os.kill(os.getpid(), signal.SIGKILL)


# --- lease ------------------------------------------------------------------------------


def acquire_lease(conn: sqlite3.Connection, owner: str, ttl_ms: int = LEASE_TTL_MS) -> Optional[Lease]:
    """
    Take the lease if it is free or expired, with a new generation. Returns None while
    someone else (or this owner's previous process) holds an unexpired lease: a restarted
    process waits out its own old lease too, so every process lifetime has its own
    generation.
    """
    now = now_ms()
    with transaction(conn):
        updated = conn.execute(
            "UPDATE lease SET owner = ?, generation = generation + 1, expires_at = ? WHERE name = ? AND expires_at < ?",
            (owner, now + ttl_ms, LEASE_NAME, now),
        ).rowcount
        if updated != 1:
            return None
        generation = conn.execute("SELECT generation FROM lease WHERE name = ?", (LEASE_NAME,)).fetchone()[0]
    return Lease(owner, generation)


def renew_lease(conn: sqlite3.Connection, lease: Lease, ttl_ms: int = LEASE_TTL_MS) -> bool:
    """Extend the lease. False means it was taken over: the caller must stop at once."""
    with transaction(conn):
        return _renew(conn, lease, ttl_ms)


def _renew(conn: sqlite3.Connection, lease: Lease, ttl_ms: int) -> bool:
    return (
        conn.execute(
            "UPDATE lease SET expires_at = ? WHERE name = ? AND owner = ? AND generation = ?",
            (now_ms() + ttl_ms, LEASE_NAME, lease.owner, lease.generation),
        ).rowcount
        == 1
    )


def release_lease(conn: sqlite3.Connection, lease: Lease) -> None:
    """Give the lease up (on clean shutdown) so the next process need not wait out the TTL."""
    with transaction(conn):
        conn.execute(
            "UPDATE lease SET expires_at = 0 WHERE name = ? AND owner = ? AND generation = ?",
            (LEASE_NAME, lease.owner, lease.generation),
        )


def _require_lease(conn: sqlite3.Connection, lease: Lease) -> None:
    """Inside a transaction: raise LostLease unless the lease is ours and unexpired."""
    row = conn.execute(
        "SELECT 1 FROM lease WHERE name = ? AND owner = ? AND generation = ? AND expires_at >= ?",
        (LEASE_NAME, lease.owner, lease.generation, now_ms()),
    ).fetchone()
    if row is None:
        raise LostLease(f"lease generation {lease.generation} is no longer held by {lease.owner}")


# --- jobs -------------------------------------------------------------------------------


def _insert_job(conn: sqlite3.Connection, catalog_id: str, kind: str, trigger: str, not_before: int) -> Optional[int]:
    try:
        return conn.execute(
            """
            INSERT INTO jobs (catalog_id, kind, trigger, state, requested_at, not_before)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (catalog_id, kind, trigger, now_ms(), not_before),
        ).lastrowid
    except sqlite3.IntegrityError as e:
        if "jobs.catalog_id" in str(e) or "UNIQUE" in str(e):
            return None
        raise


def enqueue_job(
    conn: sqlite3.Connection,
    catalog_id: str,
    *,
    kind: str = "refresh",
    trigger: str = "manual",
    max_runs_per_day: Optional[int] = None,
) -> tuple[Enqueued, Optional[int]]:
    """
    Queue a job if the catalog is enabled, has no open job, and is under its daily run limit.
    All three checks and the insert are one transaction, so the scheduler, the webhook and the
    CLI cannot race past them (PLAN.md section 4, run admission).

    The daily limit counts runs started in the last 24 hours (UTC-independent).
    """
    with transaction(conn):
        return admit_job(conn, catalog_id, kind=kind, trigger=trigger, max_runs_per_day=max_runs_per_day)


def admit_job(
    conn: sqlite3.Connection,
    catalog_id: str,
    *,
    kind: str = "refresh",
    trigger: str = "manual",
    max_runs_per_day: Optional[int] = None,
) -> tuple[Enqueued, Optional[int]]:
    """``enqueue_job`` inside the caller's transaction (the scheduler advances its row with it)."""
    if not conn.in_transaction:
        raise RuntimeError("admit_job() needs an open transaction")
    state = conn.execute("SELECT state FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)).fetchone()
    if state is None or state["state"] != "enabled":
        return Enqueued.NOT_ENABLED, None
    if max_runs_per_day is not None and kind == "refresh":
        started = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE catalog_id = ? AND started_at >= ? AND dry_run = 0",
            (catalog_id, now_ms() - DAY_MS),
        ).fetchone()[0]
        if started >= max_runs_per_day:
            return Enqueued.DAILY_LIMIT, None
    job_id = _insert_job(conn, catalog_id, kind, trigger, now_ms())
    if job_id is None:
        return Enqueued.ALREADY_OPEN, None
    return Enqueued.QUEUED, job_id


def claim_job(conn: sqlite3.Connection, lease: Lease, *, job_id: Optional[int] = None) -> Optional[Claim]:
    """
    Claim the oldest runnable pending job and open its run row, fenced by the lease.
    Pending jobs of a catalog that is no longer enabled are closed as superseded on the way.

    ``job_id`` claims that pending job only, ignoring ``not_before``: an operator's explicit
    CLI run of a job whose retry backoff has not elapsed yet.
    """
    now = now_ms()
    if job_id is None:
        where, params = "j.state = 'pending' AND j.not_before <= ?", (now,)
    else:
        where, params = "j.state = 'pending' AND j.id = ?", (job_id,)
    with transaction(conn):
        _require_lease(conn, lease)
        while True:
            job = conn.execute(
                f"""
                SELECT j.id, j.catalog_id, j.kind, j.trigger, j.attempt, d.state, d.config_rev, d.epoch
                FROM jobs j JOIN catalog_defs d USING (catalog_id)
                WHERE {where}
                ORDER BY j.requested_at, j.id LIMIT 1
                """,
                params,
            ).fetchone()
            if job is None:
                return None
            if job["state"] == "enabled":
                break
            conn.execute(
                "UPDATE jobs SET state = 'done', state_detail = 'superseded' WHERE id = ?",
                (job["id"],),
            )
        conn.execute(
            """
            UPDATE jobs SET state = 'running', claimed_by = ?, claim_gen = ?, heartbeat_at = ?
            WHERE id = ? AND state = 'pending'
            """,
            (lease.owner, lease.generation, now, job["id"]),
        )
        run_id = conn.execute(
            """
            INSERT INTO runs (job_id, catalog_id, config_rev, epoch, trigger, dry_run, started_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                job["id"],
                job["catalog_id"],
                job["config_rev"],
                job["epoch"],
                job["trigger"],
                int(job["kind"] == "dry_run"),
                now,
            ),
        ).lastrowid
    return Claim(
        job_id=job["id"],
        run_id=run_id,
        catalog_id=job["catalog_id"],
        kind=job["kind"],
        config_rev=job["config_rev"],
        epoch=job["epoch"],
        claim_gen=lease.generation,
        attempt=job["attempt"],
    )


def heartbeat(conn: sqlite3.Connection, lease: Lease, claim: Optional[Claim], ttl_ms: int = LEASE_TTL_MS) -> None:
    """Renew the lease and the running job's heartbeat together. Raises LostLease."""
    with transaction(conn):
        if not _renew(conn, lease, ttl_ms):
            raise LostLease("lease was taken over")
        if claim is not None:
            _touch_job(conn, claim, "UPDATE jobs SET heartbeat_at = ?", (now_ms(),))


def _touch_job(conn: sqlite3.Connection, claim: Claim, update_sql: str, params: tuple) -> None:
    """Run ``update_sql ... WHERE <this claim is still ours>``; LostLease if it is not."""
    sql = f"{update_sql} WHERE id = ? AND claim_gen = ? AND state = 'running'"
    if conn.execute(sql, (*params, claim.job_id, claim.claim_gen)).rowcount != 1:
        raise LostLease(f"job {claim.job_id} is no longer claimed by generation {claim.claim_gen}")


def _end_run(conn: sqlite3.Connection, claim: Claim, status: str, **fields) -> None:
    columns = ", ".join(f"{k} = ?" for k in fields)
    sql = f"UPDATE runs SET status = ?, finished_at = ?{', ' + columns if columns else ''} WHERE id = ?"
    conn.execute(sql, (status, now_ms(), *fields.values(), claim.run_id))


# Columns a lease holder may set on its own run row (besides status and finished_at).
RUN_FIELDS = frozenset(
    {
        "seed",
        "stage_counts_json",
        "reject_reasons_json",
        "prompt_hash",
        "model_calls",
        "tokens_in",
        "tokens_out",
        "relaxed_window",
        "repeats",
        "preview_json",
    }
)


def _run_fields(fields: Optional[dict]) -> dict:
    fields = {k: v for k, v in (fields or {}).items() if v is not None}
    unknown = set(fields) - RUN_FIELDS
    if unknown:
        raise ValueError(f"unknown run fields: {sorted(unknown)}")
    return fields


def update_run(conn: sqlite3.Connection, lease: Lease, claim: Claim, **fields) -> None:
    """Record progress on the claim's own run row (seed, prompt hash), fenced. Raises LostLease."""
    fields = _run_fields(fields)
    with transaction(conn):
        _require_lease(conn, lease)
        _touch_job(conn, claim, "UPDATE jobs SET heartbeat_at = ?", (now_ms(),))
        if fields:
            columns = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE runs SET {columns} WHERE id = ?", (*fields.values(), claim.run_id))


def _supersede(conn: sqlite3.Connection, claim: Claim, run_fields: Optional[dict] = None) -> None:
    """
    Close the job and run as superseded and, if the catalog is still enabled, queue exactly
    one replacement. The old job leaves the open-job index first, so the insert cannot
    collide with it.
    """
    _touch_job(conn, claim, "UPDATE jobs SET state = 'done', state_detail = 'superseded'", ())
    _end_run(conn, claim, "superseded", **_run_fields(run_fields))
    state = conn.execute("SELECT state FROM catalog_defs WHERE catalog_id = ?", (claim.catalog_id,)).fetchone()
    if state is not None and state["state"] == "enabled":
        _insert_job(conn, claim.catalog_id, claim.kind, "superseded", now_ms())


def commit_run(
    conn: sqlite3.Connection,
    lease: Lease,
    claim: Claim,
    items: Optional[Iterable[SnapshotItem]] = None,
    *,
    stage_counts_json: Optional[str] = None,
    reject_reasons_json: Optional[str] = None,
    run_fields: Optional[dict] = None,
) -> Outcome:
    """
    The fenced commit (PLAN.md section 4), one transaction: verify the lease; verify the
    catalog is still enabled at the run's config revision and epoch; then publish ``items``
    (a refresh) or just close the run (a dry run). If the catalog changed, nothing is
    published, the run ends superseded and one replacement job is queued.

    ``run_fields`` are further ``runs`` columns to record (see ``RUN_FIELDS``).

    Raises LostLease without writing anything if this process is fenced out.
    """
    items = list(items or [])
    fields = _run_fields(
        {**(run_fields or {}), "stage_counts_json": stage_counts_json, "reject_reasons_json": reject_reasons_json}
    )
    with transaction(conn):
        _require_lease(conn, lease)
        current = conn.execute(
            "SELECT state, config_rev, epoch FROM catalog_defs WHERE catalog_id = ?", (claim.catalog_id,)
        ).fetchone()
        unchanged = (
            current is not None
            and current["state"] == "enabled"
            and current["config_rev"] == claim.config_rev
            and current["epoch"] == claim.epoch
        )
        if not unchanged:
            _supersede(conn, claim, fields)
            return Outcome.SUPERSEDED
        if claim.kind == "dry_run":
            _touch_job(conn, claim, "UPDATE jobs SET state = 'done'", ())
            _end_run(conn, claim, "ok", **fields)
            return Outcome.DRY_RUN_OK
        _touch_job(conn, claim, "UPDATE jobs SET state = 'done'", ())
        publish_snapshot(conn, claim.catalog_id, items, config_rev=claim.config_rev, run_id=claim.run_id)
        _end_run(conn, claim, "ok", **fields)
        _failpoint("inside_commit")
    return Outcome.PUBLISHED


def finish_failed(
    conn: sqlite3.Connection,
    lease: Lease,
    claim: Claim,
    *,
    error_category: str,
    error_status: Optional[int] = None,
    transient: bool = False,
    run_fields: Optional[dict] = None,
) -> str:
    """
    End a run that did not publish. Transient causes (network, 429, 5xx, timeout) put the
    job back with a backoff of 5, 15 and 45 minutes, and fail it after the third attempt;
    anything else fails it at once. ``error_category`` is a controlled value, never provider
    text. Returns the job's new state.
    """
    with transaction(conn):
        _require_lease(conn, lease)
        _end_run(
            conn,
            claim,
            "failed",
            error_category=error_category,
            error_status=error_status,
            **_run_fields(run_fields),
        )
        return _retry_or_fail(conn, claim, transient, error_category)


def _retry_or_fail(conn: sqlite3.Connection, claim: Claim, transient: bool, detail: str) -> str:
    attempt = claim.attempt + 1
    if transient and attempt < MAX_ATTEMPTS:
        backoff = RETRY_BACKOFF_MS[min(attempt - 1, len(RETRY_BACKOFF_MS) - 1)]
        _touch_job(
            conn,
            claim,
            "UPDATE jobs SET state = 'pending', attempt = ?, not_before = ?, state_detail = ?, "
            "claimed_by = NULL, claim_gen = NULL",
            (attempt, now_ms() + backoff, detail),
        )
        return "pending"
    _touch_job(conn, claim, "UPDATE jobs SET state = 'failed', attempt = ?, state_detail = ?", (attempt, detail))
    return "failed"


def finish_cancelled(conn: sqlite3.Connection, lease: Lease, claim: Claim, run_fields: Optional[dict] = None) -> None:
    """SIGTERM or cancellation: put the job back with no attempt penalty; the run is abandoned."""
    with transaction(conn):
        _require_lease(conn, lease)
        _end_run(conn, claim, "abandoned", **_run_fields(run_fields))
        _touch_job(
            conn,
            claim,
            "UPDATE jobs SET state = 'pending', not_before = ?, claimed_by = NULL, claim_gen = NULL",
            (now_ms(),),
        )


def recover_abandoned(conn: sqlite3.Connection, lease: Lease, stale_ms: int = HEARTBEAT_STALE_MS) -> list[int]:
    """
    Recover jobs left ``running`` by a process that died or lost the lease: those claimed
    under an older lease generation, or whose heartbeat is older than ``stale_ms``. Each goes
    back to pending with ``attempt + 1`` and a backoff (or fails after the third attempt);
    its run becomes ``abandoned``. This frees the one-open-job index. Returns the job ids.
    """
    now = now_ms()
    recovered = []
    with transaction(conn):
        _require_lease(conn, lease)
        rows = conn.execute(
            """
            SELECT id, catalog_id, kind, attempt, claim_gen FROM jobs
            WHERE state = 'running' AND (claim_gen < ? OR heartbeat_at < ?)
            """,
            (lease.generation, now - stale_ms),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE runs SET status = 'abandoned', finished_at = ? WHERE job_id = ? AND status = 'running'",
                (now, row["id"]),
            )
            stale = Claim(
                job_id=row["id"],
                run_id=0,
                catalog_id=row["catalog_id"],
                kind=row["kind"],
                config_rev="",
                epoch=0,
                claim_gen=row["claim_gen"],
                attempt=row["attempt"],
            )
            _retry_or_fail(conn, stale, transient=True, detail="abandoned")
            recovered.append(row["id"])
    return recovered


def clear_runs(conn: sqlite3.Connection, catalog_id: str) -> int:
    """
    Delete a catalog's finished runs that nothing else needs, for the editor's "Clear" button.
    Kept: runs that published a version (snapshots and activations point at them), running
    ones, and real runs from the last 24 hours (the per-catalog daily limit counts those).
    Ledger rows stay, unlinked, so the daily AI request count is unchanged. Returns the count.
    """
    with transaction(conn):
        keep = (
            "status = 'running' OR (dry_run = 0 AND started_at >= ?) "
            "OR id IN (SELECT run_id FROM snapshots WHERE run_id IS NOT NULL) "
            "OR id IN (SELECT run_id FROM activations WHERE run_id IS NOT NULL)"
        )
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM runs WHERE catalog_id = ? AND NOT ({keep})", (catalog_id, now_ms() - DAY_MS)
            )
        ]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE llm_ledger SET run_id = NULL WHERE run_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM runs WHERE id IN ({marks})", ids)
        return len(ids)
