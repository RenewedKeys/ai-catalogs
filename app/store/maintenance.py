"""
Maintenance operations: rollback and history clear (PLAN.md section 4).

Each runs in one ``BEGIN IMMEDIATE`` transaction and bumps the catalog's ``epoch``, so a
refresh that started before it fails its commit check and ends ``superseded`` (with one
replacement job queued) instead of publishing a list selected against the old history.
They need no worker lease.

Invariant after every operation (``check_invariant``): the active snapshot's activation is
not revoked, so the list being served is always inside the history window.
"""

import sqlite3
from dataclasses import dataclass
from typing import Optional

from app.store.db import now_ms, transaction


class MaintenanceError(ValueError):
    """The operation was refused; nothing was changed."""


@dataclass(frozen=True)
class RollbackResult:
    snapshot_id: int
    previous_snapshot_id: Optional[int]
    revoked: int


def _bump_epoch(conn: sqlite3.Connection, catalog_id: str) -> None:
    if conn.execute("UPDATE catalog_defs SET epoch = epoch + 1 WHERE catalog_id = ?", (catalog_id,)).rowcount != 1:
        raise MaintenanceError(f"unknown catalog {catalog_id!r}")


def _active(conn: sqlite3.Connection, catalog_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.id AS snapshot_id, a.id AS activation_id
        FROM snapshots s LEFT JOIN activations a ON a.snapshot_id = s.id
        WHERE s.catalog_id = ? AND s.status = 'active'
        ORDER BY a.id DESC LIMIT 1
        """,
        (catalog_id,),
    ).fetchone()


def rollback(conn: sqlite3.Connection, catalog_id: str, snapshot_id: int) -> RollbackResult:
    """
    Make snapshot ``snapshot_id`` the active list again. Refused (nothing changes) unless the
    snapshot belongs to the catalog, still has its payload, is not already active, and its
    activation is not revoked. Every activation after the target's own is revoked (rows are
    kept for audit); no new activation is written, so the target keeps its place in the
    history window.
    """
    with transaction(conn):
        if conn.execute("SELECT 1 FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)).fetchone() is None:
            raise MaintenanceError(f"unknown catalog {catalog_id!r}")
        target = conn.execute(
            """
            SELECT s.id, s.status, s.metas_json IS NOT NULL AS has_payload, a.id AS activation_id, a.revoked_at
            FROM snapshots s LEFT JOIN activations a ON a.snapshot_id = s.id
            WHERE s.id = ? AND s.catalog_id = ?
            """,
            (snapshot_id, catalog_id),
        ).fetchone()
        if target is None:
            raise MaintenanceError(f"snapshot {snapshot_id} does not belong to catalog {catalog_id!r}")
        if target["status"] == "active":
            raise MaintenanceError(f"snapshot {snapshot_id} is already the active list")
        if not target["has_payload"]:
            raise MaintenanceError(f"snapshot {snapshot_id} no longer has its payload (only the last 10 are kept)")
        if target["activation_id"] is None or target["revoked_at"] is not None:
            raise MaintenanceError(
                f"snapshot {snapshot_id} was revoked by a later rollback or history clear; run a refresh instead"
            )
        current = _active(conn, catalog_id)
        now = now_ms()
        conn.execute(
            "UPDATE snapshots SET status = 'superseded' WHERE catalog_id = ? AND status = 'active'", (catalog_id,)
        )
        conn.execute("UPDATE snapshots SET status = 'active' WHERE id = ?", (snapshot_id,))
        revoked = conn.execute(
            "UPDATE activations SET revoked_at = ? WHERE catalog_id = ? AND id > ? AND revoked_at IS NULL",
            (now, catalog_id, target["activation_id"]),
        ).rowcount
        _bump_epoch(conn, catalog_id)
        check_invariant(conn, catalog_id)
    return RollbackResult(snapshot_id, current["snapshot_id"] if current else None, revoked)


def history_clear(conn: sqlite3.Connection, catalog_id: str) -> int:
    """
    Revoke every activation of the catalog except the active snapshot's, so titles shown
    before become eligible again while the list being served stays excluded from the next
    refresh. Rows are kept. Older snapshots stop being rollback targets. Returns the number
    of activations revoked.
    """
    with transaction(conn):
        if conn.execute("SELECT 1 FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)).fetchone() is None:
            raise MaintenanceError(f"unknown catalog {catalog_id!r}")
        current = _active(conn, catalog_id)
        keep = current["activation_id"] if current and current["activation_id"] is not None else -1
        revoked = conn.execute(
            "UPDATE activations SET revoked_at = ? WHERE catalog_id = ? AND id != ? AND revoked_at IS NULL",
            (now_ms(), catalog_id, keep),
        ).rowcount
        _bump_epoch(conn, catalog_id)
        check_invariant(conn, catalog_id)
    return revoked


def check_invariant(conn: sqlite3.Connection, catalog_id: str) -> None:
    """Raise ``AssertionError`` if the catalog's active snapshot has a revoked (or no) activation."""
    current = conn.execute(
        "SELECT id FROM snapshots WHERE catalog_id = ? AND status = 'active'", (catalog_id,)
    ).fetchone()
    if current is None:
        return
    live = conn.execute(
        "SELECT 1 FROM activations WHERE snapshot_id = ? AND revoked_at IS NULL", (current["id"],)
    ).fetchone()
    if live is None:
        raise AssertionError(f"catalog {catalog_id!r}: active snapshot {current['id']} has no live activation")
