"""
The shared leased job runner (PLAN.md sections 4 and 14): heartbeat, cancellation, the
wall-clock cap, lost ownership, recovery at start and maintenance during a run.
"""

import asyncio

import pytest

from app.core.config import settings
from app.curation.runner import Runner
from app.store import db
from app.store.catalogs import accept_definition, active_metas
from app.store.jobs import acquire_lease, enqueue_job
from app.store.maintenance import history_clear
from tests.unit.curation.llm_fake import API_KEY_SENTINEL
from tests.unit.curation.pipeline_fakes import (
    FakeServices,
    ScriptedLLM,
    SlowServices,
    UniverseTMDB,
    definition,
    make_universe,
)

CID = "example"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://llm.invalid/api/v1")


@pytest.fixture
def films():
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, CID, "Example", definition())
    return make_universe(400)


def enqueue(kind="refresh"):
    with db.session() as conn:
        return enqueue_job(conn, CID, kind=kind)[1]


def job_and_run(job_id):
    with db.session() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        run = conn.execute("SELECT * FROM runs WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        return dict(job), dict(run) if run else None


async def until_running(timeout=10.0):
    """Wait until the claimed run is in progress (fixed sleeps flake on a loaded CI runner)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        with db.session() as conn:
            row = conn.execute("SELECT seed FROM runs WHERE status = 'running'").fetchone()
        if row is not None and row["seed"]:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("run did not start")


def slow(films, delay):
    return SlowServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films), delay)


async def test_run_longer_than_the_lease_ttl_keeps_the_lease_and_commits(films):
    job_id = enqueue()
    runner = Runner(owner="a", services=slow(films, 0.8), lease_ttl_ms=1000, heartbeat_s=0.1)
    assert await runner.start()
    task = asyncio.create_task(runner.run_next())
    await asyncio.sleep(2.0)  # two TTLs into a ~2.4 s run
    with db.session() as conn:
        assert acquire_lease(conn, "b", 1000) is None  # a second worker cannot take over
    report = await task
    await runner.stop()
    assert report.outcome == "published"
    job, run = job_and_run(job_id)
    assert job["state"] == "done" and run["status"] == "ok"


async def test_wall_clock_cap_fails_the_run_as_transient_timeout(films):
    job_id = enqueue()
    runner = Runner(owner="a", services=slow(films, 5), heartbeat_s=0.05, wall_clock_s=0.2)
    assert await runner.start()
    report = await runner.run_next()
    await runner.stop()
    assert report.outcome == "failed" and report.category == "timeout"
    job, run = job_and_run(job_id)
    assert job["state"] == "pending" and job["attempt"] == 1  # retried later with backoff
    assert run["status"] == "failed" and run["error_category"] == "timeout"


async def test_signal_cancellation_puts_the_job_back_without_penalty(films):
    job_id = enqueue()
    runner = Runner(owner="a", services=slow(films, 5), heartbeat_s=0.05)
    assert await runner.start()
    task = asyncio.create_task(runner.run_next())
    await until_running()
    runner.cancel_current("signal")
    report = await task
    await runner.stop()
    assert report.outcome == "cancelled"
    job, run = job_and_run(job_id)
    assert job["state"] == "pending" and job["attempt"] == 0 and job["claim_gen"] is None
    assert run["status"] == "abandoned"
    with db.session() as conn:
        assert conn.execute("SELECT expires_at FROM lease").fetchone()[0] == 0  # released
        assert active_metas(conn, CID) is None


async def test_lost_lease_cancels_the_run_and_writes_nothing(films):
    job_id = enqueue()
    runner = Runner(owner="a", services=slow(films, 5), lease_ttl_ms=2000, heartbeat_s=0.05)
    assert await runner.start()
    task = asyncio.create_task(runner.run_next())
    await until_running()
    with db.session() as conn, db.transaction(conn):  # another process takes over the lease
        conn.execute(
            "UPDATE lease SET owner = 'b', generation = generation + 1, expires_at = ?", (db.now_ms() + 60_000,)
        )
    report = await task
    await runner.stop()
    assert report.outcome == "lost"
    job, run = job_and_run(job_id)
    assert job["state"] == "running" and run["status"] == "running"  # left for the new owner's recovery
    with db.session() as conn:
        assert conn.execute("SELECT owner FROM lease").fetchone()[0] == "b"  # not released by the loser


async def test_start_recovers_abandoned_jobs(films):
    job_id = enqueue()
    with db.session() as conn:
        # Long enough to claim the job even on a slow CI machine (a 1 ms lease could expire
        # between acquire and claim), short enough to wait out below.
        dead = acquire_lease(conn, "dead", 200)
        from app.store.jobs import claim_job

        claim_job(conn, dead)
    await asyncio.sleep(0.3)  # the dead holder's 200 ms lease expires
    runner = Runner(owner="a", services=FakeServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films)))
    assert await runner.start()
    job, run = job_and_run(job_id)
    assert job["state"] == "pending" and job["attempt"] == 1
    assert run["status"] == "abandoned"
    # The operator's explicit run claims it despite the retry backoff.
    report = await runner.run_next(job_id)
    await runner.stop()
    assert report.outcome == "published"


async def test_acquire_gives_up_when_the_holder_is_alive(films):
    with db.session() as conn:
        holder = acquire_lease(conn, "worker", 60_000)
    runner = Runner(owner="cli")

    async def renew():
        from app.store.jobs import renew_lease

        for _ in range(10):
            await asyncio.sleep(0.05)
            with db.session() as conn:
                renew_lease(conn, holder, 60_000)

    renewer = asyncio.create_task(renew())
    assert not await runner.acquire(wait_s=2, poll_s=0.02)
    await renewer


async def test_acquire_waits_out_a_dead_holder(films):
    with db.session() as conn:
        acquire_lease(conn, "dead", 200)
    runner = Runner(owner="cli")
    assert await runner.acquire(wait_s=2, poll_s=0.02)
    assert runner.lease.generation == 2


async def test_history_clear_during_a_run_supersedes_it(films):
    job_id = enqueue()
    services = FakeServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films))
    original = services.llm.handle

    def clear_then_answer(request):
        if services.llm.calls == 0:
            with db.session() as conn:
                history_clear(conn, CID)  # bumps the epoch
        return original(request)

    services.llm.handle = clear_then_answer
    runner = Runner(owner="a", services=services)
    assert await runner.start()
    report = await runner.run_next()
    await runner.stop()
    assert report.outcome == "superseded"
    with db.session() as conn:
        assert active_metas(conn, CID) is None
        states = [tuple(r) for r in conn.execute("SELECT id, state FROM jobs ORDER BY id")]
    assert states == [(job_id, "done"), (job_id + 1, "pending")]


async def test_heartbeats_racing_the_commit_never_report_a_lost_lease(films):
    services = FakeServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films))
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, CID, "Example", definition(target_count=5, overgenerate_factor=1.0))
    runner = Runner(owner="a", services=services, heartbeat_s=0.001)
    assert await runner.start()
    for _ in range(20):
        enqueue(kind="dry_run")
        report = await runner.run_next()
        assert report.outcome == "dry_run_ok"
        assert not runner.lost and not runner._heartbeat_task.done()
    await runner.stop()
    with db.session() as conn:
        assert conn.execute("SELECT expires_at FROM lease").fetchone()[0] == 0


async def test_rollback_during_a_run_supersedes_it(films):
    from app.store.catalogs import SnapshotItem, publish_snapshot
    from app.store.maintenance import rollback

    with db.session() as conn, db.transaction(conn):
        rev = conn.execute("SELECT config_rev FROM catalog_defs").fetchone()[0]
        first = publish_snapshot(conn, CID, [SnapshotItem("tt1", "Old A", 1990, 1)], config_rev=rev)
        publish_snapshot(conn, CID, [SnapshotItem("tt2", "Old B", 1991, 2)], config_rev=rev)
    job_id = enqueue()
    services = FakeServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films))
    original = services.llm.handle

    def rollback_then_answer(request):
        if services.llm.calls == 0:
            with db.session() as conn:
                rollback(conn, CID, first.snapshot_id)
        return original(request)

    services.llm.handle = rollback_then_answer
    runner = Runner(owner="a", services=services)
    assert await runner.start()
    report = await runner.run_next()
    await runner.stop()
    assert report.outcome == "superseded"
    with db.session() as conn:
        assert [m["id"] for m in active_metas(conn, CID)] == ["tt1"]  # the rollback stands
        states = [tuple(r) for r in conn.execute("SELECT id, state FROM jobs ORDER BY id")]
    assert states == [(job_id, "done"), (job_id + 1, "pending")]


async def test_stop_waits_for_an_in_flight_renewal_before_releasing(monkeypatch):
    """A renewal still running in the heartbeat thread must not re-extend a released lease."""
    import threading
    import time as _time

    import app.curation.runner as runner_module

    real = runner_module.heartbeat
    in_flight = threading.Event()

    def slow_heartbeat(conn, lease, claim, ttl):
        in_flight.set()
        _time.sleep(0.3)
        real(conn, lease, claim, ttl)

    monkeypatch.setattr(runner_module, "heartbeat", slow_heartbeat)
    runner = Runner(owner="a", heartbeat_s=0.01)
    assert await runner.start()
    await asyncio.to_thread(in_flight.wait, 5)
    await runner.stop()
    await asyncio.sleep(0.4)  # long enough for an undrained renewal to land
    with db.session() as conn:
        assert conn.execute("SELECT expires_at FROM lease").fetchone()[0] == 0
