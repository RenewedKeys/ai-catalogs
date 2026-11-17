"""
Backups (PLAN.md section 12): online backup, restore, pre-migration and nightly backups with
pruning, and the CLI ``backup`` / ``status`` / ``status --check`` commands.
"""

import json
import os
import shutil
import socket
import sqlite3

import pytest

from app.core.config import settings
from app.curation.__main__ import main
from app.store import db
from app.store.backup import (
    NIGHTLY,
    PRE_MIGRATION,
    automatic_backups,
    backup_database,
    backup_dir,
    nightly_backup,
    prune,
)
from app.store.catalogs import SnapshotItem, accept_definition, publish_snapshot
from app.store.jobs import acquire_lease
from app.store.maintenance import history_clear
from app.store.schema import LATEST_VERSION, MIGRATIONS


@pytest.fixture(autouse=True)
def _backups(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path / "backups"))


def publish(n: int, catalog_id="example"):
    with db.session() as conn, db.transaction(conn):
        rev = accept_definition(conn, catalog_id, "Example", {"prompt": "p"})
        items = [SnapshotItem(f"tt{1_000_000 + n * 10 + i}", f"Film {n}-{i}", 2000, n * 10 + i) for i in range(3)]
        return publish_snapshot(conn, catalog_id, items, config_rev=rev).snapshot_id


def state():
    with db.session() as conn:
        active = conn.execute("SELECT id, metas_json FROM snapshots WHERE status = 'active'").fetchone()
        history = conn.execute(
            "SELECT h.activation_id, h.imdb_id, a.revoked_at FROM history h JOIN activations a "
            "ON a.id = h.activation_id ORDER BY h.activation_id, h.imdb_id"
        ).fetchall()
        return tuple(active), [tuple(r) for r in history]


def test_backup_mutate_restore_returns_active_snapshot_and_history(tmp_path):
    publish(1)
    publish(2)
    before = state()
    dest = backup_database(tmp_path / "manual.sqlite")
    with sqlite3.connect(dest) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    publish(3)  # mutate: a new list and history, then revoke history
    with db.session() as conn:
        history_clear(conn, "example")
    assert state() != before

    # Restore as documented: stop the services, replace the file, remove -wal/-shm, start.
    live = settings.DATABASE_PATH
    for suffix in ("-wal", "-shm"):
        if os.path.exists(live + suffix):
            os.remove(live + suffix)
    shutil.copyfile(dest, live)
    assert state() == before


def test_backup_while_a_writer_holds_a_transaction(tmp_path):
    publish(1)
    writer = db.connect()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE catalog_defs SET name = 'uncommitted'")
    try:
        dest = backup_database(tmp_path / "online.sqlite")
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    with sqlite3.connect(dest) as copy:
        assert copy.execute("SELECT name FROM catalog_defs").fetchone()[0] == "Example"  # committed data only


def _database_at(path: str, version: int) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
    for v, statements in MIGRATIONS:
        if v > version:
            break
        for statement in statements:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version VALUES (?, 0)", (v,))
    conn.close()


def test_upgrade_takes_a_pre_migration_backup_of_the_old_schema(tmp_path):
    previous = MIGRATIONS[-2][0]
    path = str(tmp_path / "old.sqlite")
    _database_at(path, previous)
    conn = db.connect(path)
    try:
        assert db.migrate(conn) == LATEST_VERSION
    finally:
        conn.close()
    backups = automatic_backups(backup_dir(path), PRE_MIGRATION)
    assert len(backups) == 1 and backups[0][1].name.startswith(f"pre-migration-v{previous}-")
    with sqlite3.connect(backups[0][1]) as copy:
        assert copy.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == previous


def test_fresh_and_current_databases_take_no_pre_migration_backup(tmp_path):
    with db.session():
        pass
    with db.session():
        pass
    assert automatic_backups(backup_dir(), PRE_MIGRATION) == []


def test_nightly_backup_once_a_day_keeps_seven_and_never_prunes_manual(monkeypatch, tmp_path):
    t = {"ms": 1_800_000_000_000}
    monkeypatch.setattr(db, "clock", lambda: t["ms"] / 1000)
    publish(1)
    manual = backup_database(backup_dir() / "manual.sqlite")
    assert nightly_backup(db.now_ms()) is not None
    assert nightly_backup(db.now_ms() + 3600_000) is None  # not a day old yet
    for day in range(1, 10):
        assert nightly_backup(db.now_ms() + day * 86_400_000) is not None
    nightly = automatic_backups(backup_dir(), NIGHTLY)
    assert len(nightly) == 7
    assert manual.exists()
    assert prune(backup_dir(), NIGHTLY, keep=2) and len(automatic_backups(backup_dir(), NIGHTLY)) == 2


# --------------------------------------------------------------------------- CLI


def test_cli_backup(tmp_path, capsys):
    publish(1)
    assert main(["backup", str(tmp_path / "out" / "cli.sqlite")]) == 0
    assert "backup written" in capsys.readouterr().out
    with sqlite3.connect(tmp_path / "out" / "cli.sqlite") as copy:
        assert copy.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_cli_status_text_and_json(capsys):
    publish(1)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "example [enabled]: 3 items" in out and "lease not held" in out
    assert main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["catalogs"][0]["active_snapshot"]["items"] == 3


def test_status_check_needs_this_hosts_worker_holding_the_lease(capsys):
    assert main(["status", "--check"]) == 1  # no database yet
    with db.session() as conn:
        assert main(["status", "--check"]) == 1  # nobody holds the lease
        lease = acquire_lease(conn, f"cli-{socket.gethostname()}-1", 60_000)
        assert main(["status", "--check"]) == 1  # a CLI run is not the worker
        conn.execute("UPDATE lease SET expires_at = 0")
        acquire_lease(conn, f"worker-{socket.gethostname()}-1", 60_000)
        assert main(["status", "--check"]) == 0
        conn.execute("UPDATE lease SET owner = 'worker-otherhost-1'")
        assert main(["status", "--check"]) == 1  # another host's worker
        conn.execute("UPDATE lease SET owner = ?, expires_at = 1", (f"worker-{socket.gethostname()}-1",))
        assert main(["status", "--check"]) == 1  # expired: heartbeat stopped
    assert lease is not None
