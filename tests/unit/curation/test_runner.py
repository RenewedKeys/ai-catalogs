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


def slow(films, delay):
    return SlowServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films), delay)


async def test_run_longer_than_the_lease_ttl_keeps_the_lease_and_commits(films):
    job_id = enqueue()
    runner = Runner(owner="a", services=slow(films, 0.25), lease_ttl_ms=300, heartbeat_s=0.05)
    assert await runner.start()
    task = asyncio.create_task(runner.run_next())
    await asyncio.sleep(0.6)  # two TTLs into a ~0.75 s run
    with db.session() as conn:
        assert acquire_lease(conn, "b", 300) is None  # a second worker cannot take over
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
    await asyncio.sleep(0.2)
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
    runner = Runner(owner="a", services=slow(films, 5), lease_ttl_ms=200, heartbeat_s=0.05)
    assert await runner.start()
    task = asyncio.create_task(runner.run_next())
    await asyncio.sleep(0.15)
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
        dead = acquire_lease(conn, "dead", 1)
        from app.store.jobs import claim_job

        claim_job(conn, dead)
    await asyncio.sleep(0.01)  # the dead holder's 1 ms lease expires
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
        holder = acquire_lease(conn, "worker", 400)
    runner = Runner(owner="cli")

    async def renew():
        from app.store.jobs import renew_lease

        for _ in range(10):
            await asyncio.sleep(0.05)
            with db.session() as conn:
                renew_lease(conn, holder, 400)

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
