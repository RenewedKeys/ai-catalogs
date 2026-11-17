"""
Crash recovery with real processes (PLAN.md section 4): a worker killed with SIGKILL between
claim and commit, or inside the commit transaction, leaves the previous list live and its
job recoverable by the next lease holder.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, active_metas, publish_snapshot
from app.store.jobs import acquire_lease, claim_job, commit_run, enqueue_job, recover_abandoned, Outcome

CHILD = os.path.join(os.path.dirname(__file__), "crash_child.py")
TTL_MS = 800

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs SIGKILL")


def counts(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "snapshots": q("SELECT COUNT(*) FROM snapshots"),
        "activations": q("SELECT COUNT(*) FROM activations"),
        "history": q("SELECT COUNT(*) FROM history"),
    }


@pytest.mark.parametrize("stage", ["after_claim", "inside_commit"])
def test_sigkill_leaves_the_previous_list_and_a_recoverable_job(_temporary_database, stage):
    path = _temporary_database
    with db.session(path) as conn:
        with db.transaction(conn):
            rev = accept_definition(conn, "cult_picks", "Cult Picks", {"v": 1})
            publish_snapshot(
                conn,
                "cult_picks",
                [SnapshotItem(f"tt{100 + n}", f"Old {n}", 1999, n + 1) for n in range(10)],
                config_rev=rev,
            )
        _, job_id = enqueue_job(conn, "cult_picks")
        before = counts(conn)

    child = subprocess.run(
        [sys.executable, CHILD, path, stage, str(TTL_MS)], capture_output=True, text=True, timeout=60
    )
    assert child.returncode == -signal.SIGKILL, child.stderr

    with db.session(path) as conn:
        # Nothing of the interrupted commit is visible; the old list is still served.
        assert counts(conn) == before
        assert [m["id"] for m in active_metas(conn, "cult_picks")][:2] == ["tt100", "tt101"]
        row = conn.execute("SELECT state, claim_gen FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert row["state"] == "running" and row["claim_gen"] == 1
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "running"

        # The next process waits out the dead one's lease, then recovers the job.
        assert acquire_lease(conn, "next") is None
        time.sleep(TTL_MS / 1000 + 0.2)
        lease = acquire_lease(conn, "next")
        assert lease is not None and lease.generation == 2
        assert recover_abandoned(conn, lease) == [job_id]
        assert conn.execute("SELECT status FROM runs").fetchone()[0] == "abandoned"
        row = conn.execute("SELECT state, attempt, not_before FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert (row["state"], row["attempt"]) == ("pending", 1)

        # After the backoff the job runs to completion under the new generation.
        with db.transaction(conn):
            conn.execute("UPDATE jobs SET not_before = 0 WHERE id = ?", (job_id,))
        claim = claim_job(conn, lease)
        assert claim.job_id == job_id and claim.claim_gen == 2
        new = [SnapshotItem(f"tt{200 + n}", f"New {n}", 2005, 1000 + n) for n in range(10)]
        assert commit_run(conn, lease, claim, new) is Outcome.PUBLISHED
        assert active_metas(conn, "cult_picks")[0]["id"] == "tt200"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
