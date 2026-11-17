"""
Worker service: ``python -m app.worker`` (PLAN.md sections 4, 11 and 12).

A long-lived loop around the shared leased job runner (``app.curation.runner.Runner``); it adds
no second job path. Every job, whoever queued it (schedule, catch-up, ``/ops``, CLI), runs
through ``Runner.run_next``.

Start:
    apply ``catalogs.d`` (errors are recorded, the loop starts anyway), then take the lease,
    waiting out at most one TTL for a previous holder (``Runner.start`` also recovers
    abandoned jobs), then catch-up: one job for each scheduled catalog without an active
    snapshot or with one older than its schedule's interval.

Every ``WORKER_TICK_S`` (30 s):
    re-apply definitions when SIGHUP asked for it, a file changed (name, mtime, size), or the
    last apply left a file unapplied (retried at most every 10 minutes, e.g. TMDB was down);
    catch-up for catalogs that just got a schedule; recover abandoned jobs; queue due
    scheduled jobs and advance their ``next_run_at``; take the nightly backup when due; run
    queued jobs one at a time until none is runnable.

SIGTERM / SIGINT:
    stop claiming, cancel the in-flight run (it publishes nothing; its job goes back to
    pending without an attempt penalty), stop the heartbeat, release the lease, exit 0. The
    cancellation is immediate, well inside compose's 30 s grace period.

Lost lease (``runner.lost``):
    the runner has already cancelled the run and written nothing; the loop stops claiming,
    stops the runner and tries to take the lease again every tick.

Health: the heartbeat task renews the lease every 20 s, including during a run;
``python -m app.curation status --check`` (the compose health check) reads it.
"""

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from app.core.config import settings
from app.core.logging import describe_error
from app.core.privileges import drop_root
from app.curation.definitions import LoaderError, apply_dir
from app.curation.runner import RunReport, Runner, Services, default_owner
from app.curation.schedule import catch_up, enqueue_due, sync_schedules
from app.store import db
from app.store.backup import nightly_backup
from app.store.jobs import Enqueued, LostLease, recover_abandoned

logger = logging.getLogger("ai_catalogs.worker")

APPLY_RETRY_S = 600.0


def dir_signature(directory: str) -> Optional[tuple]:
    """What the worker compares to notice edited, added or removed definition files."""
    path = Path(directory)
    if not path.is_dir():
        return None
    try:
        entries = []
        for p in path.iterdir():
            if p.suffix in (".yaml", ".yml") and not p.name.startswith(".") and p.is_file():
                st = p.stat()
                entries.append((p.name, st.st_mtime_ns, st.st_size))
        return tuple(sorted(entries))
    except OSError:
        return None


class Worker:
    def __init__(
        self,
        runner: Optional[Runner] = None,
        *,
        services: Optional[Services] = None,
        directory: Optional[str] = None,
        tick_s: Optional[float] = None,
        db_path: Optional[str] = None,
        apply_retry_s: float = APPLY_RETRY_S,
    ):
        self.runner = runner or Runner(
            owner=default_owner("worker"), services=services or Services(db_path), db_path=db_path
        )
        self.db_path = self.runner.db_path
        self.directory = directory or settings.CATALOGS_DIR
        self.tick_s = settings.WORKER_TICK_S if tick_s is None else tick_s
        self.apply_retry_s = apply_retry_s
        self.reports: list[RunReport] = []  # for tests and logs
        self.on_report: Optional[Callable[[RunReport], None]] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._reload = False
        self._signature: Optional[tuple] = None
        self._apply_ok = True
        self._last_apply = 0.0

    # ---------------------------------------------------------------- control

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        """SIGTERM / SIGINT: stop claiming and abandon the in-flight run."""
        if not self._stop.is_set():
            logger.info("Worker stopping")
        self._stop.set()
        self._wake.set()
        self.runner.cancel_current("signal")

    def request_reload(self) -> None:
        """SIGHUP: re-apply the definition files at the next opportunity."""
        logger.info("Reload requested")
        self._reload = True
        self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep, woken early by a stop or reload request."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()
        if self._stop.is_set():
            self._wake.set()

    async def _db(self, fn, *args):
        def call():
            with db.session(self.db_path) as conn:
                return fn(conn, *args)

        return await asyncio.to_thread(call)

    # ---------------------------------------------------------------- definitions

    async def apply_definitions(self) -> None:
        self._reload = False
        self._last_apply = time.monotonic()
        self._signature = dir_signature(self.directory)
        try:
            report = await apply_dir(
                self.directory, db_path=self.db_path, tmdb_client_factory=self.runner.services.make_tmdb_client
            )
        except LoaderError as e:
            logger.warning("Definitions not applied: %s", e)
            self._apply_ok = True  # nothing to retry until the directory changes
            return
        except Exception as e:
            logger.error("Applying definitions failed: %s", describe_error(e))
            self._apply_ok = False
            return
        self._apply_ok = report.ok
        for r in report.files:
            if r.error:
                logger.warning("Definition %s not applied: %s", r.file, r.error)
            elif r.status == "ignored":
                logger.info("Definition %s ignored: catalog %s is managed in the editor", r.file, r.catalog_id)
            elif r.status == "accepted":
                logger.info("Definition %s accepted (catalog %s)", r.file, r.catalog_id)
        for catalog_id in report.removed:
            logger.info("Catalog %s removed (its file is gone)", catalog_id)

    def _reload_due(self) -> bool:
        if self._reload or dir_signature(self.directory) != self._signature:
            return True
        return not self._apply_ok and time.monotonic() - self._last_apply >= self.apply_retry_s

    # ---------------------------------------------------------------- lease

    async def _acquire(self) -> bool:
        """``Runner.start`` (waiting at most one TTL), interruptible by a stop request."""
        start = asyncio.create_task(self.runner.start(wait_s=self.runner.lease_ttl_ms / 1000))
        stop = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({start, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if not start.done():
            start.cancel()
            try:
                await start
            except (asyncio.CancelledError, Exception):
                pass
            await self.runner.stop()
            return False
        try:
            acquired = start.result()
        except Exception as e:
            logger.error("Taking the lease failed: %s", describe_error(e))
            await self.runner.stop()
            acquired = False
        if acquired:
            logger.info("Worker holds the lease (generation %d)", self.runner.lease.generation)
        return acquired

    # ---------------------------------------------------------------- loop

    async def run(self, install_signals: bool = True) -> int:
        loop = asyncio.get_running_loop()
        installed = []
        if install_signals:
            handlers = {signal.SIGTERM: self.request_stop, signal.SIGINT: self.request_stop}
            if hasattr(signal, "SIGHUP"):
                handlers[signal.SIGHUP] = self.request_reload
            for sig, handler in handlers.items():
                try:
                    loop.add_signal_handler(sig, handler)
                    installed.append(sig)
                except (NotImplementedError, RuntimeError):
                    pass
        logger.info("Worker started (owner %s, definitions in %s)", self.runner.owner, self.directory)
        try:
            if not self.stopping:
                await self.apply_definitions()
            waiting_logged = False
            while not self.stopping:
                if not await self._acquire():
                    if not self.stopping:
                        if not waiting_logged:
                            logger.info("Another process holds the lease; retrying every %ss", self.tick_s)
                            waiting_logged = True
                        await self._sleep(self.tick_s)
                    continue
                waiting_logged = False
                try:
                    await self._catch_up(None)
                    while not self.stopping and not self.runner.lost:
                        await self.tick()
                        if self.stopping or self.runner.lost:
                            break
                        await self._sleep(self.tick_s)
                finally:
                    if self.runner.lost:
                        logger.error("Lease lost; stopping claims and trying to take it again")
                    await self.runner.stop()
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
        logger.info("Worker stopped")
        return 0

    async def _catch_up(self, catalog_ids: Optional[set[str]]) -> None:
        try:
            await self._db(sync_schedules)
            for catalog_id, status in await self._db(catch_up, catalog_ids):
                if status is Enqueued.QUEUED:
                    logger.info("Catch-up: queued a refresh of %s", catalog_id)
        except Exception as e:
            logger.error("Catch-up failed: %s", describe_error(e))

    async def tick(self) -> None:
        """One pass of the loop (module docstring). Errors are logged; the loop goes on."""
        try:
            if self._reload_due():
                await self.apply_definitions()
            new = await self._db(sync_schedules)
            if new:
                await self._catch_up(set(new))
            recovered = await self._db(recover_abandoned, self.runner.lease)
            if recovered:
                logger.info("Recovered abandoned job(s): %s", ", ".join(map(str, recovered)))
            for catalog_id, status in await self._db(enqueue_due):
                logger.info("Schedule: %s for %s", status.value, catalog_id)
            path = await asyncio.to_thread(nightly_backup, db.now_ms(), self.db_path)
            if path is not None:
                logger.info("Nightly backup written: %s", path.name)
        except LostLease:
            self.runner.lost = True
            return
        except Exception as e:
            logger.error("Worker tick failed: %s", describe_error(e))
        while not self.stopping and not self.runner.lost:
            report = await self._run_one()
            if report is None:
                return
            self.reports.append(report)
            if self.on_report:
                self.on_report(report)
            logger.info(
                "Run %d (%s %s): %s%s",
                report.run_id,
                report.kind,
                report.catalog_id,
                report.outcome,
                f" ({report.category})" if report.category else "",
            )

    async def _run_one(self) -> Optional[RunReport]:
        """``run_next`` as a task, so a stop request cancels it even while it is claiming."""
        task = asyncio.create_task(self.runner.run_next())
        stop = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            while not task.done():
                self.runner.cancel_current("signal")  # no-op until the run task exists
                await asyncio.wait({task}, timeout=0.05)
        finally:
            stop.cancel()
        try:
            return task.result()
        except LostLease:
            self.runner.lost = True
            return None
        except Exception as e:
            logger.error("Running a job failed: %s", describe_error(e))
            return None


def main() -> None:
    drop_root()
    sys.exit(asyncio.run(Worker().run()))


if __name__ == "__main__":
    main()
