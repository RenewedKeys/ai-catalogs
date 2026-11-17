"""
Schedules (PLAN.md section 11): validation, DST, persistence of next_run_at, due admission and
catch-up. Neutral placeholder catalogs only.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.curation.definitions import DefinitionError, validate_definition
from app.curation.schedule import (
    Schedule,
    ScheduleError,
    catch_up,
    enqueue_due,
    interval_ms,
    next_fire,
    parse_schedule,
    sync_schedules,
)
from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, publish_snapshot
from app.store.jobs import Enqueued

BERLIN = "Europe/Berlin"
NY = "America/New_York"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def local(ms_value: int, zone: str) -> datetime:
    return datetime.fromtimestamp(ms_value / 1000, tz=ZoneInfo(zone))


@pytest.fixture
def clock(monkeypatch):
    """Settable clock (epoch ms) for app.store.db.now_ms."""
    state = {"ms": ms(datetime(2026, 1, 10, 12, tzinfo=timezone.utc))}
    monkeypatch.setattr(db, "clock", lambda: state["ms"] / 1000)

    class Clock:
        def set(self, dt: datetime):
            state["ms"] = ms(dt)

        def advance(self, **kw):
            state["ms"] += int(timedelta(**kw).total_seconds() * 1000)

        @property
        def ms(self):
            return state["ms"]

    return Clock()


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "value",
    [
        {"cron": "0 6 * * *"},
        {"cron": "0 6 * * *", "timezone": BERLIN},
        {"cron": "30 5 * * MON-FRI", "timezone": NY},
        {"cron": "  0   6 * * *  "},
        {"cron": "0 0 L * *"},
    ],
)
def test_valid_schedules(value):
    assert isinstance(parse_schedule(value), Schedule)


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("0 6 * * *", "mapping"),
        ({}, "cron is required"),
        ({"cron": "0 6 * *"}, "five fields"),
        ({"cron": "0 0 6 * * *"}, "five fields"),
        ({"cron": "@daily"}, "five fields"),
        ({"cron": "R 6 * * *"}, "only numbers"),
        ({"cron": "H 6 * * *"}, "only numbers"),
        ({"cron": "61 6 * * *"}, "not a valid"),
        ({"cron": "0 0 30 2 *"}, "never fires"),
        ({"cron": "0 6 * * *", "timezone": "Mars/Olympus"}, "IANA"),
        ({"cron": "0 6 * * *", "timezone": ""}, "IANA"),
        ({"cron": "0 6 * * *", "tz": "UTC"}, "unknown key"),
    ],
)
def test_invalid_schedules(value, fragment):
    with pytest.raises(ScheduleError, match=fragment):
        parse_schedule(value)


def test_definition_validation_rejects_a_bad_schedule_and_keeps_a_good_one_verbatim():
    with pytest.raises(DefinitionError, match="schedule"):
        validate_definition({"prompt": "p", "schedule": {"cron": "every day"}})
    canonical, _, _ = validate_definition({"prompt": "p", "schedule": {"cron": "0 6 * * *"}})
    assert canonical["schedule"] == {"cron": "0 6 * * *"}  # stored as written: config_rev unchanged


# --------------------------------------------------------------------------- DST


def fires(schedule: Schedule, start: datetime, n: int) -> list[int]:
    out, at = [], ms(start)
    for _ in range(n):
        at = next_fire(schedule, at)
        out.append(at)
    return out


def test_daily_fire_inside_the_spring_gap_fires_once_per_day_after_the_gap():
    # Berlin jumps 02:00 -> 03:00 on 2026-03-29.
    s = Schedule("30 2 * * *", BERLIN)
    got = fires(s, datetime(2026, 3, 27, 12, tzinfo=ZoneInfo(BERLIN)), 4)
    days = [local(f, BERLIN) for f in got]
    assert [d.date().isoformat() for d in days] == ["2026-03-28", "2026-03-29", "2026-03-30", "2026-03-31"]
    assert (days[0].hour, days[0].minute) == (2, 30)
    assert (days[1].hour, days[1].minute) == (3, 30)  # shifted by the gap
    assert (days[2].hour, days[2].minute) == (2, 30)
    assert got == sorted(set(got))


def test_daily_fire_inside_the_autumn_overlap_fires_once_at_the_first_occurrence():
    # Berlin repeats 02:00-03:00 on 2026-10-25 (CEST, then CET).
    s = Schedule("30 2 * * *", BERLIN)
    got = fires(s, datetime(2026, 10, 23, 12, tzinfo=ZoneInfo(BERLIN)), 4)
    days = [local(f, BERLIN) for f in got]
    assert [d.date().isoformat() for d in days] == ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"]
    overlap = days[1]
    assert (overlap.hour, overlap.minute) == (2, 30) and overlap.utcoffset() == timedelta(hours=2)
    assert got == sorted(set(got))


def test_after_the_first_occurrence_of_an_overlap_time_the_next_fire_is_the_next_day():
    s = Schedule("30 2 * * *", BERLIN)
    first = next_fire(s, ms(datetime(2026, 10, 25, 0, tzinfo=timezone.utc)))
    # Ask again from inside the repeated hour (second 02:xx, CET): no second fire that day.
    second_pass = first + 3600_000 - 60_000
    assert local(second_pass, BERLIN).fold == 1 or local(second_pass, BERLIN).utcoffset() == timedelta(hours=1)
    assert local(next_fire(s, second_pass), BERLIN).date().isoformat() == "2026-10-26"


def test_hourly_schedule_across_both_transitions_is_strictly_increasing_and_never_repeats():
    s = Schedule("15 * * * *", NY)
    for start in (datetime(2026, 3, 7, 22, tzinfo=ZoneInfo(NY)), datetime(2026, 10, 31, 22, tzinfo=ZoneInfo(NY))):
        got = fires(s, start, 12)
        assert got == sorted(set(got))
        assert all(b - a <= 2 * 3600_000 for a, b in zip(got, got[1:]))


def test_fire_times_follow_local_wall_clock_not_utc():
    s = Schedule("0 6 * * *", BERLIN)
    winter = next_fire(s, ms(datetime(2026, 1, 10, tzinfo=timezone.utc)))
    summer = next_fire(s, ms(datetime(2026, 7, 10, tzinfo=timezone.utc)))
    assert datetime.fromtimestamp(winter / 1000, tz=timezone.utc).hour == 5
    assert datetime.fromtimestamp(summer / 1000, tz=timezone.utc).hour == 4


def test_interval_of_a_daily_schedule_is_a_day_and_23_hours_over_the_spring_change():
    s = Schedule("0 6 * * *", BERLIN)
    assert interval_ms(s, ms(datetime(2026, 1, 10, tzinfo=timezone.utc))) == 24 * 3600_000
    assert interval_ms(s, ms(datetime(2026, 3, 28, 5, tzinfo=ZoneInfo(BERLIN)))) == 23 * 3600_000


# --------------------------------------------------------------------------- store


def add_catalog(catalog_id="example", schedule=None, state="enabled", **extra):
    definition = {"prompt": "Suggest films.", **extra}
    if schedule is not None:
        definition["schedule"] = schedule
    with db.session() as conn, db.transaction(conn):
        return accept_definition(conn, catalog_id, "Example", definition, state=state)


def publish(catalog_id="example", rev=None):
    with db.session() as conn, db.transaction(conn):
        if rev is None:
            rev = conn.execute("SELECT config_rev FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)).fetchone()[0]
        publish_snapshot(conn, catalog_id, [SnapshotItem("tt0000001", "Film 1", 2000, 1)], config_rev=rev)


def next_run_at(catalog_id="example"):
    with db.session() as conn:
        row = conn.execute("SELECT next_run_at FROM schedule WHERE catalog_id = ?", (catalog_id,)).fetchone()
        return row[0] if row else None


def open_jobs():
    with db.session() as conn:
        return [
            (r["catalog_id"], r["trigger"])
            for r in conn.execute("SELECT * FROM jobs WHERE state IN ('pending', 'running') ORDER BY id")
        ]


def test_sync_creates_rows_for_scheduled_enabled_catalogs_only(clock):
    add_catalog("a", {"cron": "0 6 * * *"})
    add_catalog("b")  # no schedule: manual only
    add_catalog("c", {"cron": "0 6 * * *"}, state="disabled")
    with db.session() as conn:
        assert sync_schedules(conn) == ["a"]
        assert sync_schedules(conn) == []  # idempotent
    assert local(next_run_at("a"), "UTC").replace(tzinfo=None) == datetime(2026, 1, 11, 6)
    assert next_run_at("b") is None and next_run_at("c") is None


def test_editing_the_schedule_recomputes_next_run_and_removing_it_drops_the_row(clock):
    add_catalog(schedule={"cron": "0 6 * * *"})
    with db.session() as conn:
        sync_schedules(conn)
    add_catalog(schedule={"cron": "0 5 * * *", "timezone": BERLIN})
    with db.session() as conn:
        sync_schedules(conn)
    assert local(next_run_at(), BERLIN).replace(tzinfo=None) == datetime(2026, 1, 11, 5)
    add_catalog()
    with db.session() as conn:
        sync_schedules(conn)
    assert next_run_at() is None


def test_due_fire_queues_one_job_and_advances_from_now_even_after_missed_fires(clock):
    add_catalog(schedule={"cron": "0 6 * * *"})
    with db.session() as conn:
        sync_schedules(conn)
        assert enqueue_due(conn) == []  # not due yet
    clock.advance(days=3)  # three fires missed while "down"
    with db.session() as conn:
        sync_schedules(conn)  # a stale but valid fire time is kept, not recomputed
        assert enqueue_due(conn) == [("example", Enqueued.QUEUED)]
        assert enqueue_due(conn) == []  # advanced: no second job for the missed fires
    assert open_jobs() == [("example", "schedule")]
    assert next_run_at() == next_fire(Schedule("0 6 * * *"), clock.ms)


def test_due_fire_with_an_open_job_or_at_the_daily_limit_is_skipped_not_retried(clock):
    add_catalog(schedule={"cron": "0 * * * *"}, max_runs_per_day=1)
    with db.session() as conn:
        sync_schedules(conn)
        clock.advance(hours=1)
        assert enqueue_due(conn) == [("example", Enqueued.QUEUED)]
        clock.advance(hours=1)
        assert enqueue_due(conn) == [("example", Enqueued.ALREADY_OPEN)]
        assert next_run_at() > clock.ms
        conn.execute("UPDATE jobs SET state = 'done'")
        conn.execute(
            "INSERT INTO runs (catalog_id, config_rev, epoch, trigger, started_at, status) "
            "SELECT catalog_id, config_rev, epoch, 'schedule', ?, 'ok' FROM catalog_defs",
            (clock.ms,),
        )
        clock.advance(hours=1)
        assert enqueue_due(conn) == [("example", Enqueued.DAILY_LIMIT)]
        assert enqueue_due(conn) == []
    assert open_jobs() == []


def test_catch_up_queues_for_missing_or_stale_snapshots_only(clock):
    add_catalog("fresh", {"cron": "0 6 * * *"})
    add_catalog("stale", {"cron": "0 6 * * *"})
    add_catalog("empty", {"cron": "0 6 * * *"})
    add_catalog("manual")  # unscheduled: never caught up, e.g. fixture catalogs
    publish("stale")
    clock.advance(hours=25)
    publish("fresh")
    publish("manual")
    with db.session() as conn:
        result = dict(catch_up(conn))
    assert result == {"stale": Enqueued.QUEUED, "empty": Enqueued.QUEUED}
    with db.session() as conn:
        assert catch_up(conn) == [("stale", Enqueued.ALREADY_OPEN), ("empty", Enqueued.ALREADY_OPEN)]
        assert catch_up(conn, {"fresh"}) == []
