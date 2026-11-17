"""
CLI ``run`` through the shared runner (PLAN.md section 14, milestone 5): standalone runs in
real processes (longer than the lease TTL; SIGKILL and recovery) and in-process behaviour.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from app.core.config import settings
from app.curation.__main__ import main, run_command
from app.curation.runner import Runner
from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, active_metas, publish_snapshot
from app.store.jobs import acquire_lease
from tests.unit.curation.llm_fake import API_KEY_SENTINEL
from tests.unit.curation.pipeline_fakes import FakeServices, ScriptedLLM, UniverseTMDB, definition, make_universe

CHILD = os.path.join(os.path.dirname(__file__), "cli_child.py")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
CID = "example"
TTL_MS = 1500

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs SIGKILL")


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://llm.invalid/api/v1")


@pytest.fixture
def old_snapshot(_temporary_database):
    with db.session() as conn, db.transaction(conn):
        rev = accept_definition(conn, CID, "Example", definition())
        publish_snapshot(
            conn, CID, [SnapshotItem(f"tt{90 + n}", f"Old {n}", 1999, 9000 + n) for n in range(5)], config_rev=rev
        )
    return _temporary_database


def child(path, delay, **popen):
    # The child inherits PYTEST_VERSION, so it uses TestSettings (TEST_ prefix, no .env).
    env = {**os.environ, "TEST_DATABASE_PATH": path, "DATABASE_PATH": path, "PYTHONPATH": ROOT}
    env.pop("OPENAI_API_KEY", None)
    return subprocess.Popen(
        [sys.executable, CHILD, CID, str(delay), str(TTL_MS), "0.2"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen,
    )


def wait_for(predicate, proc, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if proc.poll() is not None:
            out, err = proc.communicate()
            raise AssertionError(f"child exited {proc.returncode}: {out[-2000:]} {err[-2000:]}")
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("condition not reached")


def run_state(path):
    with db.session(path) as conn:
        r = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return r["status"] if r else None


@posix_only
def test_standalone_cli_run_longer_than_the_lease_ttl_keeps_the_lease_and_commits(old_snapshot):
    path = old_snapshot
    proc = child(path, delay=1.2)  # 3 calls: ~3.6 s, more than two TTLs
    wait_for(lambda: run_state(path) == "running", proc)
    time.sleep(TTL_MS / 1000 * 1.5)
    with db.session(path) as conn:
        assert acquire_lease(conn, "intruder", TTL_MS) is None  # still held and renewed
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert "published 100 items" in out
    with db.session(path) as conn:
        assert len(active_metas(conn, CID)) == 100
        assert conn.execute("SELECT expires_at FROM lease").fetchone()[0] == 0  # released on exit


@posix_only
def test_sigkilled_cli_run_is_recovered_by_the_next_invocation(old_snapshot):
    path = old_snapshot
    proc = child(path, delay=60)
    wait_for(lambda: run_state(path) == "running", proc)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    with db.session(path) as conn:
        assert [m["id"] for m in active_metas(conn, CID)] == [f"tt{90 + n}" for n in range(5)]
        job = conn.execute("SELECT id, state FROM jobs").fetchone()
        assert job["state"] == "running"
    job_id = job["id"]

    # The next invocation waits out the dead lease (no renewals), recovers the job and runs it.
    proc = child(path, delay=0)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    assert f"running the already queued refresh job {job_id}" in out and "published 100 items" in out
    with db.session(path) as conn:
        runs = [tuple(r) for r in conn.execute("SELECT job_id, status FROM runs ORDER BY id")]
        assert runs == [(job_id, "abandoned"), (job_id, "ok")]
        assert conn.execute("SELECT state, attempt FROM jobs WHERE id = ?", (job_id,)).fetchone()[:] == ("done", 1)
        assert len(active_metas(conn, CID)) == 100


@posix_only
def test_sigterm_abandons_the_run_and_requeues_the_job(old_snapshot):
    path = old_snapshot
    proc = child(path, delay=60)
    wait_for(lambda: run_state(path) == "running", proc)
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 1, err
    assert "cancelled" in out
    with db.session(path) as conn:
        assert conn.execute("SELECT state, attempt FROM jobs").fetchone()[:] == ("pending", 0)
        assert run_state(path) == "abandoned"
        assert conn.execute("SELECT expires_at FROM lease").fetchone()[0] == 0
        assert len(active_metas(conn, CID)) == 5


# --------------------------------------------------------------------------- in-process


def services(films):
    return FakeServices(ScriptedLLM(sorted(films), films), UniverseTMDB(films))


async def test_run_applies_catalogs_d_first_then_publishes(tmp_path, capsys):
    (tmp_path / "example.yaml").write_text("prompt: 'Suggest {{ request_count }} films.'\ntarget_count: 20\n")
    films = make_universe(100)
    code = await run_command(CID, directory=str(tmp_path), services=services(films), install_signals=False)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "example.yaml: accepted" in out and "published 20 items" in out


async def test_dry_run_reports_and_publishes_nothing(tmp_path, capsys):
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, CID, "Example", definition(target_count=10))
    code = await run_command(CID, dry_run=True, apply_first=False, services=services(make_universe(50)))
    assert code == 0 and "dry run ok" in capsys.readouterr().out
    with db.session() as conn:
        assert active_metas(conn, CID) is None


async def test_daily_run_limit_refuses(capsys):
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, CID, "Example", definition(target_count=10, max_runs_per_day=1))
    films = make_universe(100)
    assert await run_command(CID, apply_first=False, services=services(films), install_signals=False) == 0
    assert await run_command(CID, apply_first=False, services=services(films), install_signals=False) == 3
    assert "max_runs_per_day" in capsys.readouterr().err


async def test_unknown_catalog_is_a_usage_error(capsys):
    assert await run_command("missing", apply_first=False, services=services(make_universe(1))) == 2


async def test_live_worker_keeps_the_job(capsys):
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, CID, "Example", definition(target_count=10))
    with db.session() as conn:
        holder = acquire_lease(conn, "worker", 60_000)
    runner = Runner(owner="cli", services=services(make_universe(10)), lease_ttl_ms=200)

    import asyncio

    from app.store.jobs import renew_lease

    async def renew():
        for _ in range(10):
            await asyncio.sleep(0.05)
            with db.session() as conn:
                renew_lease(conn, holder, 60_000)

    renewer = asyncio.create_task(renew())
    code = await run_command(CID, apply_first=False, runner=runner, install_signals=False)
    await renewer
    assert code == 0 and "queued for it" in capsys.readouterr().out
    with db.session() as conn:
        assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "pending"


def test_validate_and_apply_commands(tmp_path, capsys, monkeypatch):
    (tmp_path / "example.yaml").write_text("prompt: 'Suggest films.'\n")
    (tmp_path / "broken.yaml").write_text("prompt: [\n")
    assert main(["validate", "--dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "broken.yaml: ERROR invalid YAML" in out and "example.yaml: valid" in out
    assert main(["apply", "--dir", str(tmp_path)]) == 1
    assert "example.yaml: accepted" in capsys.readouterr().out
    assert main(["apply", "--dir", str(tmp_path / "missing")]) == 2
