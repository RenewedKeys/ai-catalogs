"""Lease, job claim, fenced commit and recovery (PLAN.md section 4)."""

import pytest

from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, active_metas, publish_snapshot
from app.store.jobs import (
    HEARTBEAT_STALE_MS,
    LEASE_TTL_MS,
    MAX_ATTEMPTS,
    RETRY_BACKOFF_MS,
    Enqueued,
    LostLease,
    Outcome,
    acquire_lease,
    claim_job,
    commit_run,
    enqueue_job,
    finish_cancelled,
    finish_failed,
    heartbeat,
    recover_abandoned,
    release_lease,
    renew_lease,
)

T0 = 1_800_000_000.0  # seconds


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance_ms(self, ms):
        self.t += ms / 1000


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(db, "clock", c)
    return c


@pytest.fixture
def conn(_temporary_database, clock):
    with db.session(_temporary_database) as c:
        yield c


def films(start, count):
    return [SnapshotItem(f"tt{3000000 + n}", f"Film {n}", 2000 + n % 20, 900 + n) for n in range(start, start + count)]


def define(conn, catalog_id="cult_picks", definition=None, state="enabled"):
    with db.transaction(conn):
        return accept_definition(conn, catalog_id, "Cult Picks", definition or {"v": 1}, state=state)


def snapshot_ids(conn):
    return [tuple(r) for r in conn.execute("SELECT id, status FROM snapshots ORDER BY id")]


def job(conn, job_id):
    return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def run(conn, run_id):
    return dict(conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())


def open_jobs(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('pending', 'running')").fetchone()[0]


# --- lease ------------------------------------------------------------------------------


def test_lease_is_exclusive_and_generations_increase(conn, clock):
    a = acquire_lease(conn, "a")
    assert a is not None and a.generation == 1
    assert acquire_lease(conn, "b") is None
    assert acquire_lease(conn, "a") is None  # a restarted process waits out its own old lease
    clock.advance_ms(LEASE_TTL_MS + 1)
    b = acquire_lease(conn, "b")
    assert b is not None and b.generation == 2
    assert renew_lease(conn, a) is False


def test_a_renewed_lease_cannot_be_taken_over(conn, clock):
    """A healthy run longer than the TTL keeps the lease as long as it renews."""
    a = acquire_lease(conn, "a")
    for _ in range(10):  # 10 x 20 s = 200 s > 90 s TTL
        clock.advance_ms(20_000)
        assert renew_lease(conn, a)
        assert acquire_lease(conn, "b") is None


def test_release_lets_the_next_process_start_at_once(conn):
    a = acquire_lease(conn, "a")
    release_lease(conn, a)
    assert acquire_lease(conn, "b").generation == 2


# --- enqueue ----------------------------------------------------------------------------


def test_one_open_job_per_catalog(conn):
    define(conn)
    assert enqueue_job(conn, "cult_picks")[0] is Enqueued.QUEUED
    assert enqueue_job(conn, "cult_picks") == (Enqueued.ALREADY_OPEN, None)
    assert open_jobs(conn) == 1


@pytest.mark.parametrize("state", ["disabled", "removed"])
def test_disabled_or_unknown_catalogs_are_not_queued(conn, state):
    define(conn, state=state)
    assert enqueue_job(conn, "cult_picks") == (Enqueued.NOT_ENABLED, None)
    assert enqueue_job(conn, "never_defined") == (Enqueued.NOT_ENABLED, None)


def test_daily_run_limit_is_checked_with_the_insert(conn, clock):
    define(conn)
    lease = acquire_lease(conn, "w")
    for _ in range(2):
        assert enqueue_job(conn, "cult_picks", max_runs_per_day=2)[0] is Enqueued.QUEUED
        commit_run(conn, lease, claim_job(conn, lease), films(0, 3))
    assert enqueue_job(conn, "cult_picks", max_runs_per_day=2) == (Enqueued.DAILY_LIMIT, None)
    assert enqueue_job(conn, "cult_picks", kind="dry_run", max_runs_per_day=2)[0] is Enqueued.QUEUED
    renew_lease(conn, lease)
    commit_run(conn, lease, claim_job(conn, lease))  # the dry run does not count toward the limit
    clock.advance_ms(24 * 3600_000 + 1)
    assert enqueue_job(conn, "cult_picks", max_runs_per_day=2)[0] is Enqueued.QUEUED


# --- claim and commit -------------------------------------------------------------------


def test_claim_requires_the_lease(conn, clock):
    define(conn)
    enqueue_job(conn, "cult_picks")
    lease = acquire_lease(conn, "w")
    clock.advance_ms(LEASE_TTL_MS + 1)
    with pytest.raises(LostLease):
        claim_job(conn, lease)
    assert job(conn, 1)["state"] == "pending"


def test_happy_path_publishes_and_closes_job_and_run(conn):
    rev = define(conn)
    _, job_id = enqueue_job(conn, "cult_picks", trigger="schedule")
    lease = acquire_lease(conn, "w")
    claim = claim_job(conn, lease)
    assert (claim.job_id, claim.config_rev, claim.epoch, claim.kind) == (job_id, rev, 0, "refresh")
    assert job(conn, job_id)["state"] == "running" and run(conn, claim.run_id)["status"] == "running"
    assert claim_job(conn, lease) is None

    assert commit_run(conn, lease, claim, films(0, 100)) is Outcome.PUBLISHED
    assert len(active_metas(conn, "cult_picks")) == 100
    assert job(conn, job_id)["state"] == "done"
    r = run(conn, claim.run_id)
    assert (r["status"], r["job_id"], r["trigger"]) == ("ok", job_id, "schedule")
    assert conn.execute("SELECT run_id FROM activations").fetchone()[0] == claim.run_id
    assert open_jobs(conn) == 0


def test_dry_run_mutates_nothing_but_its_run(conn):
    define(conn)
    enqueue_job(conn, "cult_picks", kind="dry_run")
    lease = acquire_lease(conn, "w")
    claim = claim_job(conn, lease)
    assert commit_run(conn, lease, claim, films(0, 100)) is Outcome.DRY_RUN_OK
    assert active_metas(conn, "cult_picks") is None
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert run(conn, claim.run_id)["dry_run"] == 1 and run(conn, claim.run_id)["status"] == "ok"


@pytest.mark.parametrize("change", ["revision", "disabled", "removed", "epoch"])
def test_config_change_mid_run_supersedes_and_queues_exactly_one_replacement(conn, change):
    define(conn, definition={"v": 1})
    lease = acquire_lease(conn, "w")
    with db.transaction(conn):
        rev = conn.execute("SELECT config_rev FROM catalog_defs").fetchone()[0]
        publish_snapshot(conn, "cult_picks", films(0, 10), config_rev=rev)
    enqueue_job(conn, "cult_picks")
    claim = claim_job(conn, lease)

    if change == "revision":
        define(conn, definition={"v": 2})
    elif change == "epoch":  # a maintenance operation (rollback, history clear)
        with db.transaction(conn):
            conn.execute("UPDATE catalog_defs SET epoch = epoch + 1")
    else:
        define(conn, definition={"v": 1}, state=change)

    assert commit_run(conn, lease, claim, films(100, 10)) is Outcome.SUPERSEDED
    assert [m["id"] for m in active_metas(conn, "cult_picks")][0] == "tt3000000"  # old list still live
    assert run(conn, claim.run_id)["status"] == "superseded"
    assert job(conn, claim.job_id)["state_detail"] == "superseded"
    pending = conn.execute("SELECT id, not_before FROM jobs WHERE state = 'pending'").fetchall()
    if change in ("revision", "epoch"):
        assert len(pending) == 1 and pending[0]["not_before"] <= db.now_ms()  # runnable now
        replacement = claim_job(conn, lease)
        assert replacement.job_id == pending[0]["id"]
        assert commit_run(conn, lease, replacement, films(100, 10)) is Outcome.PUBLISHED
    else:
        assert pending == []


def test_claim_skips_jobs_of_a_catalog_disabled_while_queued(conn):
    define(conn)
    enqueue_job(conn, "cult_picks")
    define(conn, state="disabled")
    lease = acquire_lease(conn, "w")
    assert claim_job(conn, lease) is None
    assert job(conn, 1)["state_detail"] == "superseded" and open_jobs(conn) == 0


# --- fencing ----------------------------------------------------------------------------


def test_a_fenced_out_worker_writes_nothing(conn, clock):
    define(conn)
    enqueue_job(conn, "cult_picks")
    old = acquire_lease(conn, "old")
    claim = claim_job(conn, old)

    clock.advance_ms(LEASE_TTL_MS + 1)  # old pauses past its TTL
    new = acquire_lease(conn, "new")
    assert recover_abandoned(conn, new) == [claim.job_id]
    clock.advance_ms(RETRY_BACKOFF_MS[0])
    assert renew_lease(conn, new)  # the heartbeat task would have kept renewing
    reclaim = claim_job(conn, new)
    assert reclaim.job_id == claim.job_id

    before = (snapshot_ids(conn), job(conn, claim.job_id), run(conn, claim.run_id))
    for fenced in (
        lambda: commit_run(conn, old, claim, films(0, 10)),
        lambda: finish_failed(conn, old, claim, error_category="timeout", transient=True),
        lambda: finish_cancelled(conn, old, claim),
        lambda: heartbeat(conn, old, claim),
        lambda: claim_job(conn, old),
        lambda: recover_abandoned(conn, old),
    ):
        with pytest.raises(LostLease):
            fenced()
        assert (snapshot_ids(conn), job(conn, claim.job_id), run(conn, claim.run_id)) == before

    assert commit_run(conn, new, reclaim, films(0, 10)) is Outcome.PUBLISHED


def test_a_job_reclaimed_under_the_same_generation_is_fenced_by_claim_gen(conn):
    """Even with a valid lease, a stale Claim object cannot touch a job claimed again since."""
    define(conn)
    enqueue_job(conn, "cult_picks")
    lease = acquire_lease(conn, "w")
    first = claim_job(conn, lease)
    finish_cancelled(conn, lease, first)
    second = claim_job(conn, lease)
    stale = first.__class__(**{**first.__dict__, "claim_gen": first.claim_gen - 1})
    with pytest.raises(LostLease):
        commit_run(conn, lease, stale, films(0, 3))
    assert commit_run(conn, lease, second, films(0, 3)) is Outcome.PUBLISHED


# --- failures and recovery --------------------------------------------------------------


def test_transient_failures_back_off_then_fail(conn, clock):
    define(conn)
    _, job_id = enqueue_job(conn, "cult_picks")
    lease = acquire_lease(conn, "w")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claim = claim_job(conn, lease)
        assert claim is not None and claim.attempt == attempt - 1
        state = finish_failed(conn, lease, claim, error_category="provider_http_5xx", error_status=503, transient=True)
        r = run(conn, claim.run_id)
        assert (r["status"], r["error_category"], r["error_status"]) == ("failed", "provider_http_5xx", 503)
        if attempt < MAX_ATTEMPTS:
            assert state == "pending"
            assert claim_job(conn, lease) is None  # backing off
            clock.advance_ms(RETRY_BACKOFF_MS[attempt - 1])
            renew_lease(conn, lease)
        else:
            assert state == "failed"
    assert job(conn, job_id)["attempt"] == MAX_ATTEMPTS and open_jobs(conn) == 0


def test_non_transient_failure_fails_at_once(conn):
    define(conn)
    enqueue_job(conn, "cult_picks")
    lease = acquire_lease(conn, "w")
    claim = claim_job(conn, lease)
    assert finish_failed(conn, lease, claim, error_category="provider_auth", error_status=401) == "failed"
    assert open_jobs(conn) == 0
    assert enqueue_job(conn, "cult_picks")[0] is Enqueued.QUEUED


def test_cancel_returns_the_job_without_penalty(conn):
    define(conn)
    enqueue_job(conn, "cult_picks")
    lease = acquire_lease(conn, "w")
    claim = claim_job(conn, lease)
    finish_cancelled(conn, lease, claim)
    assert run(conn, claim.run_id)["status"] == "abandoned"
    again = claim_job(conn, lease)
    assert again.job_id == claim.job_id and again.attempt == 0


def test_stale_heartbeat_is_recovered_by_the_same_generation(conn, clock):
    define(conn)
    enqueue_job(conn, "cult_picks")
    lease = acquire_lease(conn, "w")
    claim = claim_job(conn, lease)
    clock.advance_ms(HEARTBEAT_STALE_MS - 1)
    heartbeat(conn, lease, claim)
    assert recover_abandoned(conn, lease) == []
    clock.advance_ms(HEARTBEAT_STALE_MS + 1)
    renew_lease(conn, lease)  # lease alive, job heartbeat not: a hung task
    assert recover_abandoned(conn, lease) == [claim.job_id]
    assert run(conn, claim.run_id)["status"] == "abandoned"
    assert job(conn, claim.job_id)["attempt"] == 1


def test_recovery_frees_the_open_job_index_after_max_attempts(conn, clock):
    define(conn)
    enqueue_job(conn, "cult_picks")
    for n in range(MAX_ATTEMPTS):
        clock.advance_ms(LEASE_TTL_MS + RETRY_BACKOFF_MS[-1] + 1)
        lease = acquire_lease(conn, f"w{n}")
        recover_abandoned(conn, lease)
        clock.advance_ms(RETRY_BACKOFF_MS[-1] + 1)
        renew_lease(conn, lease)
        if claim_job(conn, lease) is None:
            break
    clock.advance_ms(LEASE_TTL_MS + 1)
    lease = acquire_lease(conn, "last")
    recover_abandoned(conn, lease)
    assert job(conn, 1)["state"] == "failed"
    assert enqueue_job(conn, "cult_picks")[0] is Enqueued.QUEUED
