"""
Spend control: one budget gate for every outbound model attempt (PLAN.md section 7).

Every attempt, whatever its path (generation batch, top-up, format downgrade, transient
retry, whole-run retry, manual refresh, CLI run, dry run, probe), first calls
``Budget.reserve()``. That counts the ledger and inserts an ``llm_ledger`` row in **one**
``BEGIN IMMEDIATE`` transaction, so web, worker and CLI processes cannot race past a limit,
and because the row is written *before* the call, limits hold across crashes and restarts.
``Reservation.finish()`` then records the outcome and token counts.

Limits:
- ``max_model_calls_per_run`` (per catalog, default 8): rows with this run's ``run_id``
  (a run without a ``run_id``, e.g. the probe, is counted in memory);
- ``max_model_calls_per_day`` (global, ``MAX_MODEL_CALLS_PER_DAY``, default 40): all rows
  of the UTC day of ``db.now_ms()``;
- ``max_runs_per_day`` (per catalog, default 4) is run admission, enforced where jobs are
  inserted: ``app.store.jobs.enqueue_job(max_runs_per_day=...)``. ``BudgetLimits`` carries
  it so both come from the same definition.

Every row counts, whatever its status: a ``reserved`` row left by a killed process was an
attempt that may have reached the provider (and may be billed).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from app.core.config import settings
from app.store import db

logger = logging.getLogger(__name__)

DEFAULT_MAX_MODEL_CALLS_PER_RUN = 8
DEFAULT_MAX_RUNS_PER_DAY = 4


class BudgetExceeded(Exception):
    """A limit refused an attempt. Non-transient: the run fails with category ``budget``."""

    category = "budget"
    status_code = None

    def __init__(self, limit: str, used: int, allowed: int):
        self.limit = limit
        self.used = used
        self.allowed = allowed
        super().__init__(f"budget: {limit} reached ({used}/{allowed})")


@dataclass(frozen=True)
class BudgetLimits:
    max_model_calls_per_run: int = DEFAULT_MAX_MODEL_CALLS_PER_RUN
    max_model_calls_per_day: int | None = None  # None: settings.MAX_MODEL_CALLS_PER_DAY
    max_runs_per_day: int = DEFAULT_MAX_RUNS_PER_DAY

    @classmethod
    def from_definition(cls, definition: Mapping[str, Any]) -> "BudgetLimits":
        def _int(key: str, default: int) -> int:
            value = definition.get(key)
            if value is None:
                return default
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{key} must be a whole number >= 0")
            return value

        return cls(
            max_model_calls_per_run=_int("max_model_calls_per_run", DEFAULT_MAX_MODEL_CALLS_PER_RUN),
            max_runs_per_day=_int("max_runs_per_day", DEFAULT_MAX_RUNS_PER_DAY),
        )

    @property
    def per_day(self) -> int:
        return (
            settings.MAX_MODEL_CALLS_PER_DAY if self.max_model_calls_per_day is None else self.max_model_calls_per_day
        )


def ledger_day(ms: int | None = None) -> str:
    """The UTC calendar day of a millisecond timestamp (default: ``db.now_ms()``)."""
    ms = db.now_ms() if ms is None else ms
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


@dataclass
class Reservation:
    id: int
    budget: "Budget"
    finished: bool = False

    async def finish(self, ok: bool, tokens_in: int = 0, tokens_out: int = 0) -> None:
        if self.finished:
            return
        self.finished = True
        await asyncio.to_thread(self.budget._finish, self.id, ok, int(tokens_in or 0), int(tokens_out or 0))


@dataclass
class Budget:
    """The gate for one run (or one probe/CLI session when ``run_id`` is None)."""

    limits: BudgetLimits = field(default_factory=BudgetLimits)
    catalog_id: str | None = None
    run_id: int | None = None
    db_path: str | None = None
    _local_calls: int = 0

    async def reserve(self, kind: str, model: str) -> Reservation:
        """Count, check and insert a ``reserved`` ledger row; raises ``BudgetExceeded``."""
        row_id = await asyncio.to_thread(self._reserve, kind, model)
        return Reservation(row_id, self)

    def _reserve(self, kind: str, model: str) -> int:
        day = ledger_day()
        with db.session(self.db_path) as conn, db.transaction(conn):
            used_day = conn.execute("SELECT COUNT(*) FROM llm_ledger WHERE day = ?", (day,)).fetchone()[0]
            if used_day >= self.limits.per_day:
                raise BudgetExceeded("max_model_calls_per_day", used_day, self.limits.per_day)
            if self.run_id is not None:
                used_run = conn.execute("SELECT COUNT(*) FROM llm_ledger WHERE run_id = ?", (self.run_id,)).fetchone()[
                    0
                ]
            else:
                used_run = self._local_calls
            if used_run >= self.limits.max_model_calls_per_run:
                raise BudgetExceeded("max_model_calls_per_run", used_run, self.limits.max_model_calls_per_run)
            row_id = conn.execute(
                "INSERT INTO llm_ledger (day, catalog_id, run_id, at, kind, model, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'reserved')",
                (day, self.catalog_id, self.run_id, db.now_ms(), kind, model),
            ).lastrowid
        self._local_calls += 1
        return row_id

    def _finish(self, row_id: int, ok: bool, tokens_in: int, tokens_out: int) -> None:
        with db.session(self.db_path) as conn, db.transaction(conn):
            conn.execute(
                "UPDATE llm_ledger SET status = ?, tokens_in = ?, tokens_out = ? WHERE id = ?",
                ("done" if ok else "failed", tokens_in, tokens_out, row_id),
            )

    def usage(self) -> dict[str, int]:
        """Attempts and tokens of this run so far (for ``runs.model_calls`` / tokens)."""
        with db.session(self.db_path) as conn:
            if self.run_id is None:
                return {"model_calls": self._local_calls, "tokens_in": 0, "tokens_out": 0}
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0) "
                "FROM llm_ledger WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()
        return {"model_calls": row[0], "tokens_in": row[1], "tokens_out": row[2]}


def calls_today(db_path: str | None = None) -> int:
    """Attempts recorded for the current UTC day (status page)."""
    with db.session(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM llm_ledger WHERE day = ?", (ledger_day(),)).fetchone()[0]
