"""
The worker loop (PLAN.md sections 11 and 14, milestone 6) with fake LLM and TMDB: start-up
apply + catch-up, periodic recovery, scheduled admission, reload on file change and SIGHUP,
SIGTERM during a run, lost lease and re-acquisition. Neutral placeholder catalogs only.
"""

import asyncio
import os
import signal
import time

import pytest

from app.core.config import settings
from app.curation.runner import Runner
from app.curation.schedule import Schedule, next_fire
from app.store import db
from app.store.catalogs import active_metas
from app.store.jobs import enqueue_job
from app.worker import Worker
from tests.unit.curation.llm_fake import API_KEY_SENTINEL
from tests.unit.curation.pipeline_fakes import FakeServices, ScriptedLLM, SlowServices, UniverseTMDB, make_universe

CID = "example"
YAML = 'prompt: "Suggest {{ request_count }} films for category alpha."\n'
DAILY = YAML + 'schedule:\n  cron: "0 6 * * *"\n  timezone: "UTC"\n'


@pytest.fixture(autouse=True)
def _key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://llm.invalid/api/v1")
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path / "backups"))


@pytest.fixture
def films():
    return make_universe(400)


@pytest.fixture
def catalogs(tmp_path):
    d = tmp_path / "catalogs.d"
    d.mkdir()
    return d


def services(films, delay=None):
    llm, tmdb = ScriptedLLM(sorted(films), films), UniverseTMDB(films)
    return FakeServices(llm, tmdb) if delay is None else SlowServices(llm, tmdb, delay)


def make_worker(catalogs, svc, *, tick_s=0.05, ttl_ms=2000, owner="worker-test-1", **kw):
    runner = Runner(owner=owner, services=svc, lease_ttl_ms=ttl_ms, heartbeat_s=0.05)
    return Worker(runner, directory=str(catalogs), tick_s=tick_s, **kw)


async def until(pred, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def q(sql, *args):
    with db.session() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def lease():
    return q("SELECT owner, generation, expires_at FROM lease")[0]


async def stop(worker, task, timeout=10.0):
    worker.request_stop()
    return await asyncio.wait_for(task, timeout)


# --------------------------------------------------------------------------- end to end


async def test_worker_applies_catches_up_runs_and_stops_cleanly(catalogs, films):
    (catalogs / f"{CID}.yaml").write_text(DAILY)
    worker = make_worker(catalogs, services(films))
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: worker.reports, what="the catch-up run")
    assert await stop(worker, task) == 0
    report = worker.reports[0]
    assert report.outcome == "published" and report.catalog_id == CID
    with db.session() as conn:
        assert len(active_metas(conn, CID)) == 100
    assert q("SELECT trigger, state FROM jobs") == [{"trigger": "catchup", "state": "done"}]
    assert q("SELECT next_run_at FROM schedule")[0]["next_run_at"] > db.now_ms()
    assert lease()["expires_at"] == 0  # released


async def test_unscheduled_catalogs_are_not_caught_up_but_ops_jobs_run(catalogs, films):
    (catalogs / f"{CID}.yaml").write_text(YAML)
    worker = make_worker(catalogs, services(films))
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: lease()["expires_at"] > 0, what="the lease")
    await asyncio.sleep(0.3)
    assert q("SELECT * FROM jobs") == []
    with db.session() as conn:
        enqueue_job(conn, CID, kind="dry_run", trigger="ops")
    await until(lambda: worker.reports, what="the queued dry run")
    await stop(worker, task)
    assert worker.reports[0].outcome == "dry_run_ok"


async def test_due_schedule_is_admitted_by_the_loop(catalogs, films):
    (catalogs / f"{CID}.yaml").write_text(DAILY)
    worker = make_worker(catalogs, services(films))
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: len(worker.reports) == 1, what="the catch-up run")
    past_fire = next_fire(Schedule("0 6 * * *"), db.now_ms() - 86_400_000)
    with db.session() as conn:  # a fire time has passed (as after a restart, or simply at 06:00)
        conn.execute("UPDATE schedule SET next_run_at = ?", (past_fire,))
    await until(lambda: len(worker.reports) == 2, what="the scheduled run")
    await stop(worker, task)
    assert [r["trigger"] for r in q("SELECT trigger FROM jobs ORDER BY id")] == ["catchup", "schedule"]
    assert q("SELECT next_run_at FROM schedule")[0]["next_run_at"] > db.now_ms()


# --------------------------------------------------------------------------- recovery, reload


async def test_periodic_recovery_frees_a_job_abandoned_while_the_worker_runs(catalogs, films):
    (catalogs / f"{CID}.yaml").write_text(YAML)
    worker = make_worker(catalogs, services(films))
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: lease()["expires_at"] > 0, what="the lease")
    with db.session() as conn:  # a job left running by a dead process of an older generation
        _, job_id = enqueue_job(conn, CID)
        conn.execute("UPDATE jobs SET state = 'running', claim_gen = 0, heartbeat_at = 0 WHERE id = ?", (job_id,))
    await until(lambda: q("SELECT state FROM jobs")[0]["state"] == "pending", what="recovery")
    await stop(worker, task)
    job = q("SELECT state, attempt, not_before FROM jobs")[0]
    assert job["attempt"] == 1 and job["not_before"] > db.now_ms()  # retried later, with backoff


async def test_file_changes_and_sighup_reapply_definitions(catalogs, films):
    worker = make_worker(catalogs, services(films))
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: lease()["expires_at"] > 0, what="the lease")
    (catalogs / f"{CID}.yaml").write_text(DAILY)  # a new file: applied, scheduled, caught up
    await until(lambda: worker.reports, what="the new catalog's first run")
    assert q("SELECT state, source FROM catalog_defs") == [{"state": "enabled", "source": "file"}]

    (catalogs / f"{CID}.yaml").unlink()
    await until(lambda: q("SELECT state FROM catalog_defs")[0]["state"] == "removed", what="removal")
    await until(lambda: q("SELECT * FROM schedule") == [], what="the schedule row to go")  # same tick, after apply

    # SIGHUP path: an edit the signature cannot see (same name, size and mtime) is applied on request.
    (catalogs / "other.yaml").write_text(YAML)
    await until(lambda: len(q("SELECT * FROM catalog_defs")) == 2, what="the second file")
    st = os.stat(catalogs / "other.yaml")
    (catalogs / "other.yaml").write_text(YAML.replace("alpha", "gamma"))
    os.utime(catalogs / "other.yaml", ns=(st.st_atime_ns, st.st_mtime_ns))
    rev = q("SELECT config_rev FROM catalog_defs WHERE catalog_id = 'other'")[0]["config_rev"]
    await asyncio.sleep(0.2)
    assert q("SELECT config_rev FROM catalog_defs WHERE catalog_id = 'other'")[0]["config_rev"] == rev
    worker.request_reload()
    await until(
        lambda: q("SELECT config_rev FROM catalog_defs WHERE catalog_id = 'other'")[0]["config_rev"] != rev,
        what="the reload",
    )
    await stop(worker, task)


async def test_missing_definitions_directory_does_not_stop_the_worker(tmp_path, films):
    worker = make_worker(tmp_path / "missing", services(films))
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: lease()["expires_at"] > 0, what="the lease")
    assert await stop(worker, task) == 0


# --------------------------------------------------------------------------- signals, lease


async def test_sigterm_during_a_run_abandons_it_releases_the_lease_and_exits_0(catalogs, films):
    (catalogs / f"{CID}.yaml").write_text(DAILY)
    worker = make_worker(catalogs, services(films, delay=30))
    task = asyncio.create_task(worker.run())
    await until(lambda: q("SELECT 1 FROM runs WHERE status = 'running' AND seed IS NOT NULL"), what="a run")
    started = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 10) == 0
    assert time.monotonic() - started < 5
    assert worker.reports[-1].outcome == "cancelled"
    job = q("SELECT state, attempt, claim_gen FROM jobs")[0]
    assert job == {"state": "pending", "attempt": 0, "claim_gen": None}
    assert q("SELECT status FROM runs") == [{"status": "abandoned"}]
    assert lease()["expires_at"] == 0
    with db.session() as conn:
        assert active_metas(conn, CID) is None


async def test_stop_while_the_run_is_being_claimed_still_cancels_it(catalogs, films):
    (catalogs / f"{CID}.yaml").write_text(DAILY)
    worker = make_worker(catalogs, services(films, delay=30))
    original = worker.runner.claim

    async def claim_then_stop(job_id=None):
        claim = await original(job_id)
        if claim is not None:
            worker.request_stop()  # the signal lands before the run task exists
        return claim

    worker.runner.claim = claim_then_stop
    task = asyncio.create_task(worker.run(install_signals=False))
    assert await asyncio.wait_for(task, 10) == 0
    assert worker.reports[-1].outcome == "cancelled"
    assert lease()["expires_at"] == 0


async def test_worker_waits_for_a_live_holder_and_takes_over_when_it_stops(catalogs, films):
    from app.store.jobs import acquire_lease, release_lease, renew_lease

    with db.session() as conn:
        holder = acquire_lease(conn, "cli-other-1", 1000)
    (catalogs / f"{CID}.yaml").write_text(DAILY)
    worker = make_worker(catalogs, services(films), ttl_ms=1000)
    task = asyncio.create_task(worker.run(install_signals=False))
    for _ in range(15):
        await asyncio.sleep(0.05)
        with db.session() as conn:
            renew_lease(conn, holder, 1000)
    assert lease()["owner"] == "cli-other-1" and worker.reports == []
    with db.session() as conn:
        release_lease(conn, holder)
    await until(lambda: worker.reports, what="the run after takeover")
    assert lease()["owner"] == "worker-test-1"
    await stop(worker, task)


async def test_stop_while_waiting_for_the_lease_exits_promptly(catalogs, films):
    from app.store.jobs import acquire_lease

    with db.session() as conn:
        acquire_lease(conn, "cli-other-1", 60_000)
    worker = make_worker(catalogs, services(films), ttl_ms=60_000)
    task = asyncio.create_task(worker.run(install_signals=False))
    await asyncio.sleep(0.2)
    started = time.monotonic()
    assert await stop(worker, task, timeout=5) == 0
    assert time.monotonic() - started < 2
    assert lease()["owner"] == "cli-other-1"


async def test_lost_lease_stops_claiming_and_reacquires(catalogs, films):
    (catalogs / f"{CID}.yaml").write_text(YAML)
    worker = make_worker(catalogs, services(films), ttl_ms=500)
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: lease()["owner"] == "worker-test-1", what="the lease")
    gen = lease()["generation"]
    with db.session() as conn:  # another process takes over for a moment
        conn.execute(
            "UPDATE lease SET owner = 'other', generation = generation + 1, expires_at = ?", (db.now_ms() + 300,)
        )
        enqueue_job(conn, CID, kind="dry_run")
    await until(lambda: worker.runner.lost, timeout=5, what="loss detection")
    assert not worker.reports  # no claims while lost
    await until(lambda: lease()["owner"] == "worker-test-1" and lease()["generation"] > gen + 1, what="re-acquire")
    await until(lambda: worker.reports, what="the run after re-acquiring")
    await stop(worker, task)
    assert worker.reports[0].outcome == "dry_run_ok"


async def test_nightly_backup_is_taken_once_a_day(catalogs, films, tmp_path):
    worker = make_worker(catalogs, services(films))
    task = asyncio.create_task(worker.run(install_signals=False))
    await until(lambda: (tmp_path / "backups").is_dir() and list((tmp_path / "backups").iterdir()), what="backup")
    await asyncio.sleep(0.3)  # several ticks: still one backup
    await stop(worker, task)
    names = [p.name for p in (tmp_path / "backups").iterdir()]
    assert len(names) == 1 and names[0].startswith("nightly-")
