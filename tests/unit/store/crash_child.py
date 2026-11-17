"""
Child process for test_crash.py. Takes the lease, claims the queued job, then dies by SIGKILL
at the requested point:

    after_claim     between claim and commit (the run is in progress)
    inside_commit   inside the commit transaction, after every write, before COMMIT

Usage: python crash_child.py DB_PATH STAGE LEASE_TTL_MS
"""

import os
import signal
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from app.store.catalogs import SnapshotItem  # noqa: E402
from app.store.db import session  # noqa: E402
from app.store.jobs import acquire_lease, claim_job, commit_run  # noqa: E402


def main(path: str, stage: str, ttl_ms: int) -> None:
    with session(path) as conn:
        lease = acquire_lease(conn, "child", ttl_ms)
        assert lease is not None, "lease busy"
        claim = claim_job(conn, lease)
        assert claim is not None, "nothing to claim"
        if stage == "after_claim":
            os.kill(os.getpid(), signal.SIGKILL)
        os.environ["AI_CATALOGS_TEST_FAILPOINT"] = "inside_commit"
        items = [SnapshotItem(f"tt{9000000 + n}", f"New {n}", 2001, 99000 + n) for n in range(20)]
        commit_run(conn, lease, claim, items)
    raise SystemExit("unreachable: the failpoint should have killed this process")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]))
