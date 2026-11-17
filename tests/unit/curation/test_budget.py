"""Budget gate: a ledger row before every attempt, limits across restarts and processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.curation.budget import Budget, BudgetExceeded, BudgetLimits, calls_today, ledger_day
from app.store import db
from app.store.catalogs import accept_definition

ROOT = Path(__file__).resolve().parents[3]
DAY0 = 1_767_225_600.0  # 2026-01-01T00:00:00Z


@pytest.fixture
def clock(monkeypatch):
    now = {"t": DAY0 + 3600}
    monkeypatch.setattr(db, "clock", lambda: now["t"])
    return now


def make_run(catalog_id="catalog_a") -> int:
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, catalog_id, catalog_id, {"prompt": "placeholder"})
        return conn.execute(
            "INSERT INTO runs (catalog_id, config_rev, epoch, trigger, started_at, status) "
            "VALUES (?, 'rev', 0, 'manual', ?, 'running')",
            (catalog_id, db.now_ms()),
        ).lastrowid


def ledger_rows():
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_ledger ORDER BY id")]


async def test_reserve_writes_row_before_the_call_and_finish_updates_it(clock):
    run_id = make_run()
    budget = Budget(BudgetLimits(max_model_calls_per_run=3, max_model_calls_per_day=10), "catalog_a", run_id)
    res = await budget.reserve("generate", "model-x")
    (row,) = ledger_rows()
    assert row["status"] == "reserved" and row["run_id"] == run_id and row["kind"] == "generate"
    assert row["day"] == "2026-01-01" and row["model"] == "model-x"
    await res.finish(True, 120, 340)
    (row,) = ledger_rows()
    assert (row["status"], row["tokens_in"], row["tokens_out"]) == ("done", 120, 340)
    assert budget.usage() == {"model_calls": 1, "tokens_in": 120, "tokens_out": 340}


async def test_per_run_limit_counts_every_status(clock):
    run_id = make_run()
    budget = Budget(BudgetLimits(max_model_calls_per_run=3, max_model_calls_per_day=100), "catalog_a", run_id)
    a = await budget.reserve("generate", "m")
    await a.finish(False)
    await budget.reserve("retry", "m")  # left 'reserved', as after a crash
    b = await budget.reserve("downgrade", "m")
    await b.finish(True)
    with pytest.raises(BudgetExceeded) as e:
        await budget.reserve("generate", "m")
    assert e.value.limit == "max_model_calls_per_run" and e.value.category == "budget"
    assert len(ledger_rows()) == 3


async def test_daily_limit_is_global_and_resets_at_utc_midnight(clock):
    limits = BudgetLimits(max_model_calls_per_run=100, max_model_calls_per_day=2)
    await Budget(limits, "catalog_a", make_run("catalog_a")).reserve("generate", "m")
    other = Budget(limits, "catalog_b", make_run("catalog_b"))
    await other.reserve("generate", "m")
    with pytest.raises(BudgetExceeded) as e:
        await other.reserve("generate", "m")
    assert e.value.limit == "max_model_calls_per_day"
    clock["t"] = DAY0 + 86400 + 1
    assert ledger_day() == "2026-01-02"
    await other.reserve("generate", "m")


async def test_daily_default_comes_from_settings(clock, monkeypatch):
    monkeypatch.setattr(settings, "MAX_MODEL_CALLS_PER_DAY", 1)
    budget = Budget(BudgetLimits(), None, None)
    await budget.reserve("probe", "m")
    with pytest.raises(BudgetExceeded):
        await budget.reserve("probe", "m")


async def test_run_without_id_counts_in_memory(clock):
    budget = Budget(BudgetLimits(max_model_calls_per_run=2, max_model_calls_per_day=50))
    await budget.reserve("probe", "m")
    await budget.reserve("probe", "m")
    with pytest.raises(BudgetExceeded):
        await budget.reserve("probe", "m")
    assert calls_today() == 2


def test_limits_survive_a_restart(_temporary_database):
    """Two separate processes spend the same daily budget; the third attempt is refused."""
    code = (
        "import asyncio, sys\n"
        "from app.core.config import settings\n"
        "settings.DATABASE_PATH = sys.argv[1]\n"
        "from app.curation.budget import Budget, BudgetLimits, BudgetExceeded\n"
        "b = Budget(BudgetLimits(max_model_calls_per_run=10, max_model_calls_per_day=2))\n"
        "try:\n"
        "    asyncio.run(b.reserve('generate', 'm'))\n"
        "    print('ok')\n"
        "except BudgetExceeded as e:\n"
        "    print(e.limit)\n"
    )
    outs = []
    for _ in range(3):
        p = subprocess.run(
            [sys.executable, "-c", code, _temporary_database],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={**os.environ, "PYTEST_VERSION": "1"},
        )
        assert p.returncode == 0, p.stderr
        outs.append(p.stdout.strip())
    assert outs == ["ok", "ok", "max_model_calls_per_day"]


def test_limits_from_definition():
    limits = BudgetLimits.from_definition({"max_model_calls_per_run": 3, "max_runs_per_day": 1})
    assert (limits.max_model_calls_per_run, limits.max_runs_per_day) == (3, 1)
    assert BudgetLimits.from_definition({}) == BudgetLimits()
    with pytest.raises(ValueError):
        BudgetLimits.from_definition({"max_model_calls_per_run": -1})
