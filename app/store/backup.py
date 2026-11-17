"""
Online backups (PLAN.md section 12) with SQLite's backup API, safe while the web and worker
services run. Never copy the live database file instead (WAL contents would be missing).

Automatic backups live in ``BACKUP_DIR`` (default ``<database dir>/backups``):

- ``nightly-<UTC timestamp>.sqlite``: taken by the worker when the newest one is a day old;
- ``pre-migration-v<N>-<UTC timestamp>.sqlite``: taken before upgrading from schema version N.

``BACKUP_KEEP`` (default 7) of each kind are kept; manual backups (``python -m app.curation
backup PATH``) are never pruned.

Restore: stop both services, replace the database file with the backup, delete the database's
``-wal`` and ``-shm`` files if present, start again.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import settings

NIGHTLY = "nightly"
PRE_MIGRATION = "pre-migration"
DAY_MS = 24 * 3600_000
_NAME_RE = re.compile(r"^(nightly|pre-migration-v\d+)-(\d{8}T\d{6}Z)(?:-\d+)?\.sqlite$")


def backup_dir(db_path: Optional[str] = None) -> Path:
    if settings.BACKUP_DIR:
        return Path(settings.BACKUP_DIR)
    return Path(os.path.abspath(db_path or settings.DATABASE_PATH)).parent / "backups"


def backup_to(source: sqlite3.Connection, dest: str | os.PathLike) -> Path:
    """
    Copy the database behind ``source`` to ``dest`` with the online backup API. Written to a
    temporary name and renamed, so ``dest`` is either complete or absent. Overwrites ``dest``.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            # A standalone file: no WAL needed next to it.
            target.execute("PRAGMA journal_mode = DELETE")
        finally:
            target.close()
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    return dest


def backup_database(dest: str | os.PathLike, db_path: Optional[str] = None) -> Path:
    """Back up the database at ``db_path`` (default: the configured one) to ``dest``."""
    from app.store.db import connect

    source = connect(db_path)
    try:
        return backup_to(source, dest)
    finally:
        source.close()


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_stamp(text: str) -> int:
    return int(datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def automatic_backups(directory: Path, kind: str) -> list[tuple[int, Path]]:
    """(timestamp ms, path) of the automatic backups of one kind, oldest first."""
    if not directory.is_dir():
        return []
    out = []
    for p in directory.iterdir():
        m = _NAME_RE.match(p.name)
        if m and (m.group(1) == kind or (kind == PRE_MIGRATION and m.group(1).startswith(PRE_MIGRATION))):
            out.append((_parse_stamp(m.group(2)), p))
    return sorted(out)


def prune(directory: Path, kind: str, keep: Optional[int] = None) -> list[Path]:
    keep = settings.BACKUP_KEEP if keep is None else keep
    backups = automatic_backups(directory, kind)
    removed = []
    for _, p in backups[: max(0, len(backups) - keep)]:
        p.unlink(missing_ok=True)
        removed.append(p)
    return removed


def _unique(directory: Path, stem: str) -> Path:
    path = directory / f"{stem}.sqlite"
    n = 1
    while path.exists():
        path = directory / f"{stem}-{n}.sqlite"
        n += 1
    return path


def take_automatic(kind_name: str, source: sqlite3.Connection, now_ms: int, db_path: Optional[str] = None) -> Path:
    directory = backup_dir(db_path)
    path = backup_to(source, _unique(directory, f"{kind_name}-{_stamp(now_ms)}"))
    prune(directory, PRE_MIGRATION if kind_name.startswith(PRE_MIGRATION) else kind_name)
    return path


def nightly_due(now_ms: int, db_path: Optional[str] = None) -> bool:
    backups = automatic_backups(backup_dir(db_path), NIGHTLY)
    return not backups or now_ms - backups[-1][0] >= DAY_MS


def nightly_backup(now_ms: int, db_path: Optional[str] = None) -> Optional[Path]:
    """Take a nightly backup if the newest is a day old (or none exists). Returns its path."""
    if not nightly_due(now_ms, db_path):
        return None
    from app.store.db import connect

    source = connect(db_path)
    try:
        return take_automatic(NIGHTLY, source, now_ms, db_path)
    finally:
        source.close()


def latest_backup(db_path: Optional[str] = None) -> Optional[tuple[int, str]]:
    """Newest automatic backup of any kind: (timestamp ms, file name)."""
    directory = backup_dir(db_path)
    found = automatic_backups(directory, NIGHTLY) + automatic_backups(directory, PRE_MIGRATION)
    if not found:
        return None
    at, path = max(found)
    return at, path.name
