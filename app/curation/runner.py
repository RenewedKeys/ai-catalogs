"""
The shared leased job runner (PLAN.md sections 4 and 14, milestone 5).

The one code path that runs a refresh or dry run: it takes the worker lease, recovers
abandoned jobs, claims a job, runs the pipeline and finalizes the job through the fenced
primitives of ``app.store.jobs``. Nothing publishes outside it. The CLI uses it for one job;
the worker (milestone 6) wraps it in its scheduling loop.

What it owns, as the lease holder:

- **Heartbeat task**: independent of the run, every ``heartbeat_s`` (20 s) it renews the
  lease and the current job's heartbeat on its own connection. If ownership is lost, or
  renewal keeps failing for longer than the lease TTL, it cancels the in-flight run; the
  runner then writes nothing (the new owner's recovery handles the job).
- **Cancellation**: ``cancel_current("signal")`` (SIGTERM/SIGINT) cancels the pipeline
  task, never the runner, which stays alive to put the job back (no attempt penalty, run
  ``abandoned``) and release the lease.
- **Wall-clock cap** (``RUN_WALL_CLOCK_S``, default 20 min): the run is cancelled and fails
  as a transient ``timeout``.
- **Recovery at start**: ``recover_abandoned`` before the first claim.

Every SQLite call runs in a thread on its own connection, so the event loop is never blocked.
"""

import asyncio
import json
import logging
import os
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.core.config import settings
from app.core.logging import describe_error
from app.curation.budget import Budget, BudgetLimits
from app.curation.llm import CuratedLLMService, LLMSettings
from app.curation.pipeline import Pipeline, PipelineFailure, PipelineResult
from app.curation.prompt import make_nonce, make_seed
from app.curation.tmdb_gate import TMDBClient, TMDBGate
from app.curation.watched import MDBListClient
from app.store import db
from app.store.jobs import (
    LEASE_NAME,
    LEASE_TTL_MS,
    Claim,
    Lease,
    LostLease,
    Outcome,
    acquire_lease,
    claim_job,
    commit_run,
    finish_cancelled,
    finish_failed,
    heartbeat,
    recover_abandoned,
    release_lease,
    update_run,
)

logger = logging.getLogger(__name__)

HEARTBEAT_S = 20.0


def default_owner(prefix: str = "cli") -> str:
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}"


class Services:
    """Builds the outbound services for one run. Tests subclass it to inject fakes."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def make_llm(self, definition: Mapping[str, Any], budget: Budget) -> Any:
        return CuratedLLMService(LLMSettings.from_definition(definition), budget, db_path=self.db_path)

    def make_gate(self) -> TMDBGate:
        return TMDBGate(db_path=self.db_path)

    def make_watched_client(self) -> MDBListClient:
        """For ``exclude_watched``; raises ``WatchedError("config")`` when no key is set."""
        return MDBListClient()

    def make_tmdb_client(self) -> TMDBClient:
        """For resolving exclusion names when definitions are applied."""
        return TMDBClient()


@dataclass
class RunReport:
    job_id: int
    run_id: int
    catalog_id: str
    kind: str
    outcome: str  # published | dry_run_ok | superseded | failed | cancelled | lost
    category: Optional[str] = None
    job_state: Optional[str] = None
    items: int = 0
    partial: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("published", "dry_run_ok")


@dataclass
class Runner:
    owner: str = field(default_factory=default_owner)
    services: Services = field(default_factory=Services)
    db_path: Optional[str] = None
    lease_ttl_ms: int = LEASE_TTL_MS
    heartbeat_s: float = HEARTBEAT_S
    wall_clock_s: float = field(default_factory=lambda: float(settings.RUN_WALL_CLOCK_S))

    lease: Optional[Lease] = field(default=None, init=False)
    lost: bool = field(default=False, init=False)
    _claim: Optional[Claim] = field(default=None, init=False)
    _task: Optional[asyncio.Task] = field(default=None, init=False)
    _cancel_reason: Optional[str] = field(default=None, init=False)
    _heartbeat_task: Optional[asyncio.Task] = field(default=None, init=False)
    # Serialises heartbeats with the job hand-over: a beat must never touch a job that the
    # runner is finalizing (it would see a job that is no longer 'running' and report a
    # false lost lease). The heartbeat also has its own thread, so a busy default pool
    # (TMDB cache lookups) cannot delay renewal.
    _beat_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _beat_executor: Optional[ThreadPoolExecutor] = field(default=None, init=False)

    # ---------------------------------------------------------------- db helpers

    async def _db(self, fn, *args, **kwargs):
        def call():
            with db.session(self.db_path) as conn:
                return fn(conn, *args, **kwargs)

        return await asyncio.to_thread(call)

    # ---------------------------------------------------------------- lease

    async def acquire(self, wait_s: float = 0.0, poll_s: float = 0.25) -> bool:
        """
        Take the lease, waiting up to ``wait_s`` for an expired one. Gives up early when the
        holder is seen renewing (a live worker): the caller then leaves the job to it. A
        holder that died stops renewing, so its lease expires within one TTL.
        """

        def read(conn):
            row = conn.execute(
                "SELECT owner, generation, expires_at FROM lease WHERE name = ?", (LEASE_NAME,)
            ).fetchone()
            return (row["owner"], row["generation"], row["expires_at"]) if row else None

        deadline = time.monotonic() + wait_s
        first = await self._db(read)
        while True:
            lease = await self._db(acquire_lease, self.owner, self.lease_ttl_ms)
            if lease is not None:
                self.lease = lease
                self.lost = False
                return True
            now = await self._db(read)
            if first is not None and now is not None and now != first:
                if now[2] < db.now_ms():
                    first = now  # released or expired in between: try again at once
                    continue
                return False  # renewed or taken over: someone alive holds it
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll_s)

    async def start(self, wait_s: float = 0.0) -> bool:
        """Acquire the lease, start the heartbeat task and recover abandoned jobs."""
        if not await self.acquire(wait_s):
            return False
        self._beat_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lease-heartbeat")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="lease-heartbeat")
        recovered = await self._db(recover_abandoned, self.lease)
        if recovered:
            logger.info("Recovered abandoned job(s): %s", ", ".join(map(str, recovered)))
        return True

    async def stop(self) -> None:
        """Stop the heartbeat and release the lease (unless it was lost)."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        if self._beat_executor is not None:
            # Cancelling the task does not stop a renewal already running in the heartbeat
            # thread; if it finished after the release below, it would extend the lease again
            # and the next process would wait out a full TTL. Let it drain first.
            await asyncio.to_thread(self._beat_executor.shutdown, True)
            self._beat_executor = None
        if self.lease is not None and not self.lost:
            try:
                await self._db(release_lease, self.lease)
            except sqlite3.Error as e:
                logger.warning("Could not release the lease: %s", describe_error(e))
        self.lease = None

    async def _beat(self) -> None:
        lease, claim, ttl = self.lease, self._claim, self.lease_ttl_ms

        def call():
            with db.session(self.db_path) as conn:
                heartbeat(conn, lease, claim, ttl)

        await asyncio.get_running_loop().run_in_executor(self._beat_executor, call)

    async def _heartbeat_loop(self) -> None:
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                async with self._beat_lock:
                    await self._beat()
                last_ok = time.monotonic()
            except LostLease:
                logger.error("Lease ownership lost; cancelling the current run")
                self._lose()
                return
            except Exception as e:  # e.g. database locked for a while
                logger.warning("Heartbeat failed: %s", describe_error(e))
                if (time.monotonic() - last_ok) * 1000 > self.lease_ttl_ms:
                    logger.error("Heartbeat failing for longer than the lease TTL; cancelling the current run")
                    self._lose()
                    return

    def _lose(self) -> None:
        self.lost = True
        self.cancel_current("lost")

    def cancel_current(self, reason: str = "signal") -> None:
        """Cancel the in-flight pipeline (signal handler, lost lease, wall-clock cap)."""
        if self._task is not None and not self._task.done():
            self._cancel_reason = self._cancel_reason or reason
            self._task.cancel()

    # ---------------------------------------------------------------- jobs

    async def claim(self, job_id: Optional[int] = None) -> Optional[Claim]:
        if self.lease is None or self.lost:
            return None
        return await self._db(claim_job, self.lease, job_id=job_id)

    async def run_next(self, job_id: Optional[int] = None) -> Optional[RunReport]:
        claim = await self.claim(job_id)
        return None if claim is None else await self.run_claim(claim)

    async def run_claim(self, claim: Claim) -> RunReport:
        """Run one claimed job to a terminal, fenced outcome."""
        async with self._beat_lock:
            self._claim = claim
        self._cancel_reason = None
        self._usage: dict[str, int] = {}
        self._task = asyncio.create_task(self._execute(claim), name=f"run-{claim.run_id}")
        try:
            done, _ = await asyncio.wait({self._task}, timeout=self.wall_clock_s)
            if not done:
                self.cancel_current("timeout")
                await asyncio.wait({self._task})
            # Hand the job over from the heartbeat to the terminal write: from here on the
            # beats renew the lease only.
            async with self._beat_lock:
                self._claim = None
            return await self._finalize(claim, self._task)
        finally:
            self._claim = None
            self._task = None

    async def _execute(self, claim: Claim) -> Optional[PipelineResult]:
        def load(conn):
            return conn.execute(
                "SELECT config_rev, definition_json, exclusions_json FROM catalog_defs WHERE catalog_id = ?",
                (claim.catalog_id,),
            ).fetchone()

        row = await self._db(load)
        if row is None or row["config_rev"] != claim.config_rev:
            return None  # changed since the claim: the commit check supersedes the run
        try:
            definition = json.loads(row["definition_json"])
            budget_limits = BudgetLimits.from_definition(definition)
        except ValueError:
            raise PipelineFailure("config", detail="stored definition is invalid") from None
        today = datetime.fromtimestamp(db.now_ms() / 1000, tz=timezone.utc).date()
        seed = make_seed(today, claim.catalog_id, make_nonce())
        await self._db(update_run, self.lease, claim, seed=seed)
        budget = Budget(budget_limits, claim.catalog_id, run_id=claim.run_id, db_path=self.db_path)
        llm = self.services.make_llm(definition, budget)
        gate = self.services.make_gate()
        try:
            pipeline = Pipeline(
                catalog_id=claim.catalog_id,
                definition=definition,
                exclusions_json=row["exclusions_json"],
                seed=seed,
                today=today,
                llm=llm,
                gate=gate,
                db_path=self.db_path,
                watched_client_factory=self.services.make_watched_client,
            )
            return await pipeline.run()
        finally:
            try:
                self._usage = await asyncio.to_thread(budget.usage)
            except Exception:
                self._usage = {}
            for closer in (getattr(llm, "aclose", None), getattr(gate, "aclose", None)):
                if closer is not None:
                    try:
                        await closer()
                    except Exception as e:
                        logger.warning("Closing a client failed: %s", describe_error(e))

    async def _finalize(self, claim: Claim, task: asyncio.Task) -> RunReport:
        report = RunReport(claim.job_id, claim.run_id, claim.catalog_id, claim.kind, outcome="failed")
        usage = dict(self._usage)
        try:
            if task.cancelled():
                reason = self._cancel_reason or "signal"
                if reason == "lost" or self.lost:
                    report.outcome, report.category = "lost", "lost_lease"
                    return report  # no writes: the new owner's recovery handles the job
                if reason == "timeout":
                    report.category = "timeout"
                    report.job_state = await self._db(
                        finish_failed,
                        self.lease,
                        claim,
                        error_category="timeout",
                        transient=claim.kind == "refresh",
                        run_fields=usage,
                    )
                    return report
                await self._db(finish_cancelled, self.lease, claim, usage)
                report.outcome, report.job_state = "cancelled", "pending"
                return report

            error = task.exception()
            if error is None:
                result: Optional[PipelineResult] = task.result()
                fields = {**usage, **(result.stats if result else {})}
                stage = fields.pop("stage_counts_json", None)
                rejects = fields.pop("reject_reasons_json", None)
                items = result.items if result and claim.kind == "refresh" else None
                if result and claim.kind == "dry_run":  # a test run keeps its picks to show them
                    fields["preview_json"] = json.dumps([[i.title, i.year, i.imdb_id] for i in result.items])
                outcome = await self._db(
                    commit_run,
                    self.lease,
                    claim,
                    items,
                    stage_counts_json=stage,
                    reject_reasons_json=rejects,
                    run_fields=fields,
                )
                report.outcome = outcome.value
                report.job_state = "done"
                report.items = len(result.items) if result else 0
                report.partial = bool(result and result.partial)
                if outcome is Outcome.SUPERSEDED:
                    report.category = "superseded"
                return report

            if isinstance(error, PipelineFailure):
                category, status, transient = error.category, error.status, error.transient
                report.detail = error.detail
                fields = {**usage, **error.stats}
            else:
                logger.error("Run %d failed unexpectedly: %s", claim.run_id, describe_error(error))
                category, status, transient, fields = "internal", None, False, usage
            report.category = category
            report.job_state = await self._db(
                finish_failed,
                self.lease,
                claim,
                error_category=category,
                error_status=status,
                transient=transient and claim.kind == "refresh",
                run_fields=fields,
            )
            return report
        except LostLease:
            self.lost = True
            report.outcome, report.category = "lost", "lost_lease"
            return report
