"""Store: migrations, publishing, retention and the history window (PLAN.md section 4)."""

import json
import multiprocessing
import sqlite3

import pytest

from app.store import db
from app.store.catalogs import (
    KEEP_PAYLOADS,
    SnapshotItem,
    StoreError,
    accept_definition,
    active_metas,
    config_rev_of,
    publish_snapshot,
    servable_catalogs,
    window_activation_ids,
)
from app.store.schema import LATEST_VERSION


def items(start: int, count: int) -> list[SnapshotItem]:
    return [SnapshotItem(f"tt{1000000 + n}", f"Film {n}", 1980 + n % 45, 500 + n) for n in range(start, start + count)]


@pytest.fixture
def conn(_temporary_database):
    with db.session(_temporary_database) as c:
        yield c


def define(conn, catalog_id="example", definition=None, **kw):
    with db.transaction(conn):
        return accept_definition(conn, catalog_id, "Example Catalog", definition or {"v": 1}, **kw)


def publish(conn, rev, batch, catalog_id="example"):
    with db.transaction(conn):
        return publish_snapshot(conn, catalog_id, batch, config_rev=rev)


# --- migrations -------------------------------------------------------------------------


def test_fresh_database_is_migrated_with_wal(conn):
    assert db.schema_version(conn) == LATEST_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert [tuple(r) for r in conn.execute("SELECT name, generation FROM lease")] == [("worker", 0)]


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == LATEST_VERSION
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == LATEST_VERSION
    assert conn.execute("SELECT COUNT(*) FROM lease").fetchone()[0] == 1


def _migrate_in_child(path, barrier, results):
    barrier.wait()
    try:
        c = db.connect(path)
        results.put(("ok", db.migrate(c)))
        c.close()
    except Exception as e:  # reported to the parent
        results.put(("error", repr(e)))


def test_concurrent_first_start_migrates_exactly_once(tmp_path):
    """Eight web workers and the worker service start together on an empty volume."""
    path = str(tmp_path / "race.sqlite")
    ctx = multiprocessing.get_context("spawn")
    n = 9
    barrier, results = ctx.Barrier(n), ctx.Queue()
    procs = [ctx.Process(target=_migrate_in_child, args=(path, barrier, results)) for _ in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    outcomes = [results.get(timeout=5) for _ in range(n)]
    assert outcomes == [("ok", LATEST_VERSION)] * n
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == LATEST_VERSION
    assert c.execute("SELECT COUNT(*) FROM lease").fetchone()[0] == 1
    c.close()


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            accept_definition(conn, "example", "Example Catalog", {"v": 1})
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM catalog_defs").fetchone()[0] == 0
    assert not conn.in_transaction


def test_writes_require_a_transaction(conn):
    with pytest.raises(RuntimeError):
        accept_definition(conn, "example", "Example Catalog", {"v": 1})


# --- definitions ------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Cult", "cult-picks", "cult__picks", "_cult", "cult_", "", "cult.picks", "kult picks"])
def test_catalog_ids_must_survive_aiometadata(conn, bad):
    with pytest.raises(StoreError):
        define(conn, bad)


def test_epoch_changes_only_with_revision_or_state(conn):
    rev = define(conn, definition={"v": 1})
    epoch = lambda: conn.execute("SELECT epoch FROM catalog_defs").fetchone()[0]  # noqa: E731
    assert rev == config_rev_of({"v": 1}) and epoch() == 0
    define(conn, definition={"v": 1})
    assert epoch() == 0
    define(conn, definition={"v": 2})
    assert epoch() == 1
    define(conn, definition={"v": 2}, state="disabled")
    assert epoch() == 2


# --- publishing -------------------------------------------------------------------------


def test_publish_swaps_the_active_snapshot_and_records_history(conn):
    rev = define(conn)
    first = publish(conn, rev, items(0, 100))
    second = publish(conn, rev, items(100, 60))
    assert len(active_metas(conn, "example")) == 60
    statuses = dict(conn.execute("SELECT id, status FROM snapshots").fetchall())
    assert statuses == {first.snapshot_id: "superseded", second.snapshot_id: "active"}
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 160
    assert window_activation_ids(conn, "example", 7) == [second.activation_id, first.activation_id]


def test_served_metas_are_the_stremio_shape(conn):
    rev = define(conn)
    publish(conn, rev, [SnapshotItem("tt0133093", "The Matrix", 1999, 603, description="Neo.")])
    assert active_metas(conn, "example") == [
        {"id": "tt0133093", "type": "movie", "name": "The Matrix", "releaseInfo": "1999", "description": "Neo."}
    ]


@pytest.mark.parametrize(
    "batch, message",
    [
        ([], "at least one"),
        (items(0, 251), "at most"),
        ([SnapshotItem("nm0000001", "X")], "invalid IMDb"),
        ([SnapshotItem("tt1", "A", tmdb_id=1), SnapshotItem("tt1", "B", tmdb_id=2)], "duplicate IMDb"),
        ([SnapshotItem("tt1", "A", tmdb_id=1), SnapshotItem("tt2", "B", tmdb_id=1)], "duplicate TMDB"),
        ([SnapshotItem("tt1", " ")], "missing title"),
    ],
)
def test_invalid_snapshots_are_refused_and_nothing_changes(conn, batch, message):
    rev = define(conn)
    publish(conn, rev, items(0, 5))
    with pytest.raises(StoreError, match=message):
        publish(conn, rev, batch)
    assert len(active_metas(conn, "example")) == 5
    assert conn.execute("SELECT COUNT(*) FROM activations").fetchone()[0] == 1


def test_publish_refuses_a_stale_revision(conn):
    old = define(conn, definition={"v": 1})
    define(conn, definition={"v": 2})
    with pytest.raises(StoreError, match="revision"):
        publish(conn, old, items(0, 5))
    assert active_metas(conn, "example") is None


def test_publish_refuses_an_unknown_catalog(conn):
    with pytest.raises(StoreError, match="unknown catalog"):
        publish(conn, "x" * 64, items(0, 5), catalog_id="nope")


def test_the_database_enforces_one_active_snapshot(conn):
    rev = define(conn)
    publish(conn, rev, items(0, 5))
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO snapshots (catalog_id, created_at, config_rev, status, item_count, metas_json) "
                "VALUES ('example', 0, ?, 'active', 0, '[]')",
                (rev,),
            )


# --- retention --------------------------------------------------------------------------


def test_payloads_are_pruned_but_history_and_activations_are_kept(conn):
    """PLAN.md section 4: payloads keep the last 10; activations and history are never pruned."""
    rev = define(conn)
    for n in range(50):
        publish(conn, rev, items(n * 100, 100))
    with_payload = conn.execute("SELECT COUNT(*) FROM snapshots WHERE metas_json IS NOT NULL").fetchone()[0]
    assert with_payload == KEEP_PAYLOADS
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 50
    assert conn.execute("SELECT COUNT(*) FROM activations").fetchone()[0] == 50
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 5000

    # A 14- and a 30-run window are fully backed by history although only 10 payloads remain.
    for window in (14, 30):
        ids = window_activation_ids(conn, "example", window)
        assert len(ids) == window
        placeholders = ",".join("?" * len(ids))
        n = conn.execute(f"SELECT COUNT(*) FROM history WHERE activation_id IN ({placeholders})", ids).fetchone()[0]
        assert n == window * 100


def test_revoked_activations_leave_the_window(conn):
    rev = define(conn)
    published = [publish(conn, rev, items(n * 10, 10)) for n in range(5)]
    with db.transaction(conn):
        conn.execute("UPDATE activations SET revoked_at = 1 WHERE id = ?", (published[3].activation_id,))
    assert window_activation_ids(conn, "example", 3) == [
        published[4].activation_id,
        published[2].activation_id,
        published[1].activation_id,
    ]


# --- manifest listing ---------------------------------------------------------------------


def test_only_enabled_catalogs_with_a_snapshot_are_servable(conn):
    rev_a = define(conn, "a_list", position=2)
    rev_b = define(conn, "b_list", position=1)
    define(conn, "no_snapshot_yet")
    rev_d = define(conn, "disabled_list")
    publish(conn, rev_a, items(0, 3), "a_list")
    publish(conn, rev_b, items(0, 3), "b_list")
    publish(conn, rev_d, items(0, 3), "disabled_list")
    define(conn, "disabled_list", state="disabled")
    assert [c.catalog_id for c in servable_catalogs(conn)] == ["b_list", "a_list"]


def test_metas_round_trip_unicode(conn):
    rev = define(conn)
    publish(conn, rev, [SnapshotItem("tt0080684", "Star Wars: Episode V – The Empire Strikes Back", 1980, 1891)])
    raw = conn.execute("SELECT metas_json FROM snapshots").fetchone()[0]
    assert "–" in raw and json.loads(raw)[0]["name"].endswith("Strikes Back")
