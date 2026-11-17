"""
SQLite connections, transactions and migrations.

Every caller gets its connection from ``connect()`` so the same settings apply everywhere:

- ``isolation_level=None``: the sqlite3 module never opens or commits a transaction on its
  own. Transactions are explicit, via ``transaction()``, and always ``BEGIN IMMEDIATE`` so
  the write lock is taken up front (no deferred-upgrade deadlocks between processes).
- WAL journal, ``synchronous=NORMAL``, foreign keys on, a busy timeout.

Times are integer milliseconds from ``now_ms()``. Tests replace ``app.store.db.clock`` to
control time.
"""

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from app.core.config import settings
from app.store.schema import LATEST_VERSION, MIGRATIONS

BUSY_TIMEOUT_MS = 5000

clock: Callable[[], float] = time.time


def now_ms() -> int:
    return int(clock() * 1000)


def database_path() -> str:
    return settings.DATABASE_PATH


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection with the project's settings. The caller closes it."""
    path = path or database_path()
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``, or ``ROLLBACK`` on any exception."""
    if conn.in_transaction:
        raise RuntimeError("transaction() does not nest")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone()
    if row is None:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """
    Bring the schema up to date. Safe to call from several processes at once (up to eight
    uvicorn workers and the worker service start together on one volume): the version is
    re-read inside the write transaction, so exactly one process applies each migration and
    the others then find nothing to do.

    Returns the schema version after the call.
    """
    # journal_mode is persistent and cannot change inside a transaction. When several
    # processes race here the losers may see "database is locked" despite the busy timeout
    # (SQLite does not retry this case); WAL is then already being set by the winner.
    for _ in range(50):
        try:
            if conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() == "wal":
                break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e):
                raise
        time.sleep(0.1)
    else:
        raise RuntimeError("could not enable WAL journal mode")

    current = schema_version(conn)
    if current >= LATEST_VERSION:
        return current
    if current > 0:
        # Upgrading a database that holds data: back it up first (PLAN.md section 12). A
        # failed backup stops the upgrade. Racing processes may each take one (unique names).
        from app.store.backup import PRE_MIGRATION, take_automatic

        path = conn.execute("PRAGMA database_list").fetchone()["file"]
        take_automatic(f"{PRE_MIGRATION}-v{current}", conn, now_ms(), path or None)

    with transaction(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)"
        )
        current = schema_version(conn)
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, now_ms()))
    return schema_version(conn)


_migrated: set[str] = set()
_migrated_lock = threading.Lock()


def open_db(path: str | None = None) -> sqlite3.Connection:
    """
    ``connect()``, after making sure the schema is current. Migration runs at most once per
    process and path; later calls just connect. Creates the parent directory if missing.
    """
    path = os.path.abspath(path or database_path())
    if path not in _migrated:
        with _migrated_lock:
            if path not in _migrated:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                conn = connect(path)
                try:
                    migrate(conn)
                finally:
                    conn.close()
                _migrated.add(path)
    return connect(path)


@contextmanager
def session(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """``with session() as conn:`` opens a migrated connection and always closes it."""
    conn = open_db(path)
    try:
        yield conn
    finally:
        conn.close()
