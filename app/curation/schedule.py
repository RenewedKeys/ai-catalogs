"""
Catalog schedules (PLAN.md section 11): cron + timezone, persisted ``schedule.next_run_at``,
due-job admission and catch-up at start.

A definition's ``schedule`` is ``{cron: "<5-field cron>", timezone: "<IANA zone>"}``;
``timezone`` defaults to UTC. A catalog without a schedule only runs when asked (CLI, ``/ops``).

**Wall-clock semantics and DST.** The cron expression is evaluated on the catalog's local
wall-clock time, and each local fire time is then resolved to an instant:

- a fire time inside a spring-forward gap (it does not exist that day) fires after the gap,
  shifted by the gap's length (02:30 in a 02:00 -> 03:00 jump fires at 03:30);
- a fire time inside an autumn overlap (it exists twice) fires once, at its first occurrence.

So a daily schedule fires exactly once per local day, and fire times are strictly increasing.

**Persistence.** ``schedule.next_run_at`` survives restarts, so a restart neither skips nor
repeats a fire: a fire missed while the worker was down is due at once (one job, however many
fires were missed), and the next one is computed from *now*. When the stored time is not a fire
time of the current schedule (the cron or timezone was edited), it is recomputed. Admission and
the advance of ``next_run_at`` are one transaction.

Only deterministic five-field cron is accepted: no seconds field, no ``@`` nicknames, no random
(``R``) or hashed (``H``) fields, because a stored fire time must be checkable against the
expression.
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter

from app.curation.budget import BudgetLimits
from app.store.db import now_ms, transaction
from app.store.jobs import Enqueued, admit_job

logger = logging.getLogger(__name__)

_NAMES = {
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    "SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT", "L",
}  # fmt: skip
_FIELD_RE = re.compile(r"[0-9A-Za-z*,/#\-]+")
_MAX_SEARCH = 2000  # candidate fire times examined per lookup (a gap/overlap skips a few)


class ScheduleError(ValueError):
    """An invalid schedule. The message is our own text."""


@dataclass(frozen=True)
class Schedule:
    cron: str
    timezone: str = "UTC"

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def parse_schedule(value: Any) -> Optional[Schedule]:
    """Validate a definition's ``schedule`` value. None stays None. Raises ``ScheduleError``."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ScheduleError("schedule must be a mapping with 'cron' and optionally 'timezone'")
    unknown = set(value) - {"cron", "timezone"}
    if unknown:
        raise ScheduleError(f"schedule: unknown key(s): {', '.join(sorted(map(str, unknown)))}")
    cron = value.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        raise ScheduleError("schedule.cron is required, e.g. '0 6 * * *' (06:00 every day)")
    fields = cron.split()
    if len(fields) != 5:
        raise ScheduleError("schedule.cron must have five fields: minute hour day-of-month month day-of-week")
    for f in fields:
        if not _FIELD_RE.fullmatch(f) or any(w.upper() not in _NAMES for w in re.findall(r"[A-Za-z]+", f)):
            raise ScheduleError("schedule.cron: only numbers, ranges, steps, lists and month/day names are allowed")
    cron = " ".join(fields)
    if not croniter.is_valid(cron):
        raise ScheduleError("schedule.cron is not a valid cron expression")
    tz = value.get("timezone", "UTC")
    if not isinstance(tz, str) or not tz.strip():
        raise ScheduleError("schedule.timezone must be an IANA zone name such as 'Europe/Berlin'")
    try:
        ZoneInfo(tz.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("schedule.timezone is not a known IANA zone name (e.g. 'Europe/Berlin', 'UTC')") from None
    schedule = Schedule(cron=cron, timezone=tz.strip())
    next_fire(schedule, now_ms())  # a cron that never fires (e.g. 30 February) is an error
    return schedule


def schedule_of(definition: Mapping[str, Any]) -> Optional[Schedule]:
    return parse_schedule(definition.get("schedule"))


def next_fire(schedule: Schedule, after_ms: int) -> int:
    """The first fire instant strictly after ``after_ms`` (epoch ms), per the module docstring."""
    zone = schedule.zone
    after = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)
    local = after.astimezone(zone).replace(tzinfo=None)
    it = croniter(schedule.cron, local)
    for _ in range(_MAX_SEARCH):
        try:
            naive = it.get_next(datetime)
        except CroniterBadDateError:
            break
        # fold=0: in an overlap the first occurrence; in a gap the pre-transition offset,
        # which places the instant after the gap, shifted by its length.
        instant = naive.replace(tzinfo=zone).astimezone(timezone.utc)
        ms = int(instant.timestamp() * 1000)
        if ms > after_ms:
            return ms
    raise ScheduleError("schedule.cron never fires")


def interval_ms(schedule: Schedule, at_ms: int) -> int:
    """The gap between the next two fires after ``at_ms`` (the catalog's refresh interval)."""
    first = next_fire(schedule, at_ms)
    return next_fire(schedule, first) - first


def is_fire_time(schedule: Schedule, ms: int) -> bool:
    return next_fire(schedule, ms - 1) == ms


# --------------------------------------------------------------------------- store


@dataclass
class ScheduledCatalog:
    catalog_id: str
    schedule: Schedule
    max_runs_per_day: Optional[int]


def scheduled_catalogs(conn: sqlite3.Connection) -> list[ScheduledCatalog]:
    """Enabled catalogs with a valid schedule. A stored definition that no longer parses is skipped."""
    out = []
    for row in conn.execute("SELECT catalog_id, definition_json FROM catalog_defs WHERE state = 'enabled'"):
        try:
            definition = json.loads(row["definition_json"])
            schedule = schedule_of(definition)
            if schedule is None:
                continue
            limits = BudgetLimits.from_definition(definition)
        except (ValueError, TypeError):
            logger.warning("Catalog %s: stored schedule is invalid; not scheduled", row["catalog_id"])
            continue
        out.append(ScheduledCatalog(row["catalog_id"], schedule, limits.max_runs_per_day))
    return out


def sync_schedules(conn: sqlite3.Connection) -> list[str]:
    """
    Make ``schedule`` match the accepted definitions: a row for every enabled catalog with a
    schedule (new rows, and rows whose time is not a fire time of the current schedule, get the
    next fire after now), none for the others. Returns the catalog ids that got a new row.
    """
    now = now_ms()
    created = []
    catalogs = {c.catalog_id: c for c in scheduled_catalogs(conn)}
    with transaction(conn):
        stored = {r["catalog_id"]: r["next_run_at"] for r in conn.execute("SELECT * FROM schedule")}
        for catalog_id in stored.keys() - catalogs.keys():
            conn.execute("DELETE FROM schedule WHERE catalog_id = ?", (catalog_id,))
        for catalog_id, c in catalogs.items():
            at = stored.get(catalog_id)
            if at is not None and is_fire_time(c.schedule, at):
                continue
            conn.execute(
                "INSERT INTO schedule (catalog_id, next_run_at) VALUES (?, ?) "
                "ON CONFLICT (catalog_id) DO UPDATE SET next_run_at = excluded.next_run_at",
                (catalog_id, next_fire(c.schedule, now)),
            )
            if at is None:
                created.append(catalog_id)
    return created


def enqueue_due(conn: sqlite3.Connection) -> list[tuple[str, Enqueued]]:
    """
    Queue one refresh for every catalog whose ``next_run_at`` has passed and advance it to the
    next fire after now, whatever the admission result (an open job or the daily limit skips
    this fire; missed fires never pile up). One transaction per catalog.
    """
    results = []
    now = now_ms()
    for c in scheduled_catalogs(conn):
        with transaction(conn):
            row = conn.execute("SELECT next_run_at FROM schedule WHERE catalog_id = ?", (c.catalog_id,)).fetchone()
            if row is None or row["next_run_at"] > now:
                continue
            status, _ = admit_job(
                conn, c.catalog_id, kind="refresh", trigger="schedule", max_runs_per_day=c.max_runs_per_day
            )
            conn.execute(
                "UPDATE schedule SET next_run_at = ? WHERE catalog_id = ?",
                (next_fire(c.schedule, now), c.catalog_id),
            )
        results.append((c.catalog_id, status))
    return results


def catch_up(conn: sqlite3.Connection, catalog_ids: Optional[set[str]] = None) -> list[tuple[str, Enqueued]]:
    """
    Queue one refresh for each scheduled catalog (optionally only ``catalog_ids``) that has no
    active snapshot, or whose active snapshot is older than its schedule's interval.
    """
    results = []
    now = now_ms()
    for c in scheduled_catalogs(conn):
        if catalog_ids is not None and c.catalog_id not in catalog_ids:
            continue
        snap = conn.execute(
            "SELECT created_at FROM snapshots WHERE catalog_id = ? AND status = 'active'", (c.catalog_id,)
        ).fetchone()
        if snap is not None and now - snap["created_at"] <= interval_ms(c.schedule, now):
            continue
        with transaction(conn):
            status, _ = admit_job(
                conn, c.catalog_id, kind="refresh", trigger="catchup", max_runs_per_day=c.max_runs_per_day
            )
        results.append((c.catalog_id, status))
    return results
