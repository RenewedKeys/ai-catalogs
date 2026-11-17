"""Rollback and history clear (PLAN.md section 4), with the invariant checked after every operation."""

import pytest

from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, active_metas, publish_snapshot, window_activation_ids
from app.store.maintenance import MaintenanceError, check_invariant, history_clear, rollback

CID = "example"


@pytest.fixture
def conn():
    with db.session() as c:
        with db.transaction(c):
            accept_definition(c, CID, "Example", {"prompt": "x"})
        yield c


def publish(conn, n: int):
    """Publish snapshot number n (items unique to it); returns (snapshot_id, activation_id)."""
    rev = conn.execute("SELECT config_rev FROM catalog_defs WHERE catalog_id = ?", (CID,)).fetchone()[0]
    items = [SnapshotItem(f"tt{n * 1000 + i}", f"Film {n}-{i}", 2000, n * 1000 + i) for i in range(3)]
    with db.transaction(conn):
        p = publish_snapshot(conn, CID, items, config_rev=rev)
    return p.snapshot_id, p.activation_id


def epoch(conn):
    return conn.execute("SELECT epoch FROM catalog_defs WHERE catalog_id = ?", (CID,)).fetchone()[0]


def active_snapshot(conn):
    return conn.execute("SELECT id FROM snapshots WHERE catalog_id = ? AND status = 'active'", (CID,)).fetchone()[0]


def test_windows_after_50_publications(conn):
    activations = [publish(conn, n)[1] for n in range(50)]
    assert window_activation_ids(conn, CID, 14) == activations[::-1][:14]
    assert window_activation_ids(conn, CID, 30) == activations[::-1][:30]
    # Raising the window later is backed by data: history is never pruned.
    assert window_activation_ids(conn, CID, 50) == activations[::-1]
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 150


def test_rollback_revokes_later_activations_and_keeps_the_target_in_the_window(conn):
    snaps = [publish(conn, n) for n in range(5)]  # A..E
    before = epoch(conn)
    result = rollback(conn, CID, snaps[1][0])
    assert result.snapshot_id == snaps[1][0] and result.previous_snapshot_id == snaps[4][0] and result.revoked == 3
    assert active_snapshot(conn) == snaps[1][0]
    assert epoch(conn) == before + 1
    assert window_activation_ids(conn, CID, 7) == [snaps[1][1], snaps[0][1]]
    assert active_metas(conn, CID)[0]["id"] == "tt1000"
    check_invariant(conn, CID)


def test_repeated_rollbacks_refuse_revoked_targets(conn):
    a, b, c = (publish(conn, n) for n in range(3))
    rollback(conn, CID, a[0])
    with pytest.raises(MaintenanceError, match="revoked"):
        rollback(conn, CID, b[0])
    with pytest.raises(MaintenanceError, match="already the active"):
        rollback(conn, CID, a[0])
    assert active_snapshot(conn) == a[0]
    check_invariant(conn, CID)


def test_history_clear_keeps_only_the_active_activation_and_blocks_older_rollbacks(conn):
    snaps = [publish(conn, n) for n in range(4)]
    before = epoch(conn)
    assert history_clear(conn, CID) == 3
    assert window_activation_ids(conn, CID, 7) == [snaps[3][1]]
    assert epoch(conn) == before + 1
    with pytest.raises(MaintenanceError, match="revoked"):
        rollback(conn, CID, snaps[2][0])
    check_invariant(conn, CID)
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 12  # rows are kept


def test_rollback_refuses_a_pruned_payload_and_foreign_snapshots(conn):
    snaps = [publish(conn, n) for n in range(12)]  # payloads of the first two are pruned
    before = epoch(conn)
    with pytest.raises(MaintenanceError, match="payload"):
        rollback(conn, CID, snaps[0][0])
    with pytest.raises(MaintenanceError, match="does not belong"):
        rollback(conn, CID, 9999)
    with pytest.raises(MaintenanceError, match="unknown catalog"):
        history_clear(conn, "missing")
    assert epoch(conn) == before and active_snapshot(conn) == snaps[11][0]


def test_cli_rollback_and_history_clear(conn, capsys):
    from app.curation.__main__ import main

    a, b = publish(conn, 0), publish(conn, 1)
    assert main(["rollback", CID, str(a[0])]) == 0
    assert "active again" in capsys.readouterr().out
    assert main(["rollback", CID, str(b[0])]) == 2
    assert "revoked" in capsys.readouterr().err
    assert main(["history", "clear", CID]) == 0
    check_invariant(conn, CID)
