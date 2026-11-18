"""
Catalog definitions, snapshots, activations and history.

Write functions here expect to run inside ``app.store.db.transaction()``; they never open
or commit one themselves, so callers can combine them with other checks (lease, epoch and
config revision in the fenced commit) in one atomic unit.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app.store.db import now_ms

CATALOG_ID_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")
IMDB_ID_RE = re.compile(r"^tt\d+$")

MEDIA_TYPES = ("movie", "series")
MAX_ITEMS = 250  # PLAN.md section 9: the whole snapshot goes out in one response
KEEP_PAYLOADS = 10  # snapshot payloads kept per catalog; activations and history are never pruned


class StoreError(ValueError):
    """Input that the store refuses to write."""


@dataclass(frozen=True)
class SnapshotItem:
    imdb_id: str
    title: str
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    poster: Optional[str] = None
    description: Optional[str] = None
    media_type: str = "movie"  # Stremio type: movie | series (PLAN.md section 29)

    def meta(self) -> dict[str, Any]:
        """The Stremio meta served for this item (PLAN.md section 9)."""
        meta: dict[str, Any] = {"id": self.imdb_id, "type": self.media_type, "name": self.title}
        if self.year:
            meta["releaseInfo"] = str(self.year)
        if self.poster:
            meta["poster"] = self.poster
        if self.description:
            meta["description"] = self.description
        return meta


@dataclass(frozen=True)
class Published:
    snapshot_id: int
    activation_id: int


def _require_tx(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("must be called inside app.store.db.transaction()")


def validate_catalog_id(catalog_id: str) -> str:
    """
    Catalog ids must survive AIOMetadata unchanged: it replaces every non-alphanumeric
    character with "_" when building its internal id, so only [a-z0-9] runs joined by single
    underscores are allowed.
    """
    if not CATALOG_ID_RE.fullmatch(catalog_id or ""):
        raise StoreError(f"invalid catalog id {catalog_id!r}: use lowercase letters and digits joined by '_'")
    return catalog_id


def config_rev_of(definition: dict[str, Any]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def accept_definition(
    conn: sqlite3.Connection,
    catalog_id: str,
    name: str,
    definition: dict[str, Any],
    *,
    state: str = "enabled",
    position: int = 0,
) -> str:
    """
    Store an accepted catalog definition. A changed revision (or state) bumps ``epoch``, so a
    run that started under the old one cannot commit (PLAN.md section 4). Returns the
    ``config_rev``.

    Milestone 2 only needs this for fixtures; the YAML loader (milestone 5) is its real caller.
    """
    _require_tx(conn)
    validate_catalog_id(catalog_id)
    if state not in ("enabled", "disabled", "removed"):
        raise StoreError(f"invalid state {state!r}")
    if not name or not name.strip():
        raise StoreError("catalog name is required")
    rev = config_rev_of(definition)
    conn.execute(
        """
        INSERT INTO catalog_defs (catalog_id, name, config_rev, definition_json, state, position, epoch, accepted_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT (catalog_id) DO UPDATE SET
            name = excluded.name,
            definition_json = excluded.definition_json,
            position = excluded.position,
            accepted_at = excluded.accepted_at,
            last_error = NULL,
            epoch = catalog_defs.epoch
                    + (catalog_defs.config_rev != excluded.config_rev OR catalog_defs.state != excluded.state),
            config_rev = excluded.config_rev,
            state = excluded.state
        """,
        (catalog_id, name.strip(), rev, json.dumps(definition, sort_keys=True), state, position, now_ms()),
    )
    return rev


def _validated_items(items: Iterable[SnapshotItem]) -> list[SnapshotItem]:
    items = list(items)
    if not items:
        raise StoreError("a snapshot needs at least one item")
    if len(items) > MAX_ITEMS:
        raise StoreError(f"a snapshot holds at most {MAX_ITEMS} items, got {len(items)}")
    imdb_seen: set[str] = set()
    tmdb_seen: set[int] = set()
    for item in items:
        if not IMDB_ID_RE.fullmatch(item.imdb_id or ""):
            raise StoreError(f"invalid IMDb id {item.imdb_id!r}")
        if item.imdb_id in imdb_seen:
            raise StoreError(f"duplicate IMDb id {item.imdb_id}")
        imdb_seen.add(item.imdb_id)
        if item.tmdb_id is not None:
            if item.tmdb_id in tmdb_seen:
                raise StoreError(f"duplicate TMDB id {item.tmdb_id}")
            tmdb_seen.add(item.tmdb_id)
        if not item.title or not item.title.strip():
            raise StoreError(f"missing title for {item.imdb_id}")
    return items


def publish_snapshot(
    conn: sqlite3.Connection,
    catalog_id: str,
    items: Iterable[SnapshotItem],
    *,
    config_rev: str,
    run_id: Optional[int] = None,
) -> Published:
    """
    Make ``items`` the catalog's active list: supersede the previous snapshot, insert the new
    one, record its activation and history rows, and prune old payloads (never history).

    Callers are responsible for the checks that decide *whether* to publish (lease, epoch,
    config revision); this only does the writes, atomically with whatever else the caller's
    transaction contains.
    """
    _require_tx(conn)
    items = _validated_items(items)
    kinds = {i.media_type for i in items}
    if len(kinds) != 1 or not kinds <= set(MEDIA_TYPES):
        raise StoreError("a snapshot holds items of one type, movie or series")
    media_type = kinds.pop()
    row = conn.execute("SELECT config_rev FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)).fetchone()
    if row is None:
        raise StoreError(f"unknown catalog {catalog_id!r}")
    if row["config_rev"] != config_rev:
        raise StoreError(f"catalog {catalog_id!r} is at revision {row['config_rev'][:12]}, not {config_rev[:12]}")

    now = now_ms()
    conn.execute("UPDATE snapshots SET status = 'superseded' WHERE catalog_id = ? AND status = 'active'", (catalog_id,))
    snapshot_id = conn.execute(
        """
        INSERT INTO snapshots (catalog_id, created_at, run_id, config_rev, status, item_count, metas_json, media_type)
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            catalog_id,
            now,
            run_id,
            config_rev,
            len(items),
            json.dumps([i.meta() for i in items], ensure_ascii=False),
            media_type,
        ),
    ).lastrowid
    activation_id = conn.execute(
        "INSERT INTO activations (catalog_id, snapshot_id, run_id, activated_at) VALUES (?, ?, ?, ?)",
        (catalog_id, snapshot_id, run_id, now),
    ).lastrowid
    conn.executemany(
        "INSERT INTO history (activation_id, catalog_id, tmdb_id, imdb_id, title, year) VALUES (?, ?, ?, ?, ?, ?)",
        [(activation_id, catalog_id, i.tmdb_id, i.imdb_id, i.title, i.year) for i in items],
    )
    conn.execute(
        """
        UPDATE snapshots SET metas_json = NULL
        WHERE catalog_id = ? AND status != 'active' AND metas_json IS NOT NULL
          AND id NOT IN (SELECT id FROM snapshots WHERE catalog_id = ? ORDER BY id DESC LIMIT ?)
        """,
        (catalog_id, catalog_id, KEEP_PAYLOADS),
    )
    return Published(snapshot_id=snapshot_id, activation_id=activation_id)


# --- reads (no transaction needed: each statement sees a consistent WAL snapshot) ----------


@dataclass(frozen=True)
class ServedCatalog:
    catalog_id: str
    name: str
    media_type: str = "movie"  # the active snapshot's type: what is actually served


def servable_catalogs(conn: sqlite3.Connection) -> list[ServedCatalog]:
    """Enabled catalogs that have an active snapshot, in manifest order (PLAN.md section 9)."""
    rows = conn.execute("""
        SELECT d.catalog_id, d.name, s.media_type FROM catalog_defs d
        JOIN snapshots s ON s.catalog_id = d.catalog_id AND s.status = 'active'
        WHERE d.state = 'enabled'
        ORDER BY d.position, d.catalog_id
        """).fetchall()
    return [ServedCatalog(r["catalog_id"], r["name"], r["media_type"]) for r in rows]


def catalog_state(conn: sqlite3.Connection, catalog_id: str) -> Optional[str]:
    row = conn.execute("SELECT state FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)).fetchone()
    return row["state"] if row else None


def active_metas(
    conn: sqlite3.Connection, catalog_id: str, media_type: Optional[str] = None
) -> Optional[list[dict[str, Any]]]:
    """
    The active snapshot's metas, or None when the catalog has no active snapshot (or, with
    ``media_type``, when the active snapshot is of another type).
    """
    row = conn.execute(
        "SELECT metas_json, media_type FROM snapshots WHERE catalog_id = ? AND status = 'active'", (catalog_id,)
    ).fetchone()
    if row is None or (media_type is not None and row["media_type"] != media_type):
        return None
    return json.loads(row["metas_json"])


def window_activation_ids(
    conn: sqlite3.Connection, catalog_id: str, runs: int, media_type: Optional[str] = None
) -> list[int]:
    """
    The last ``runs`` non-revoked activations, newest first: the history window. With
    ``media_type``, only activations of lists of that type (a catalog switched from movies to
    shows starts a fresh window: TMDB movie and TV ids are separate number spaces).
    """
    rows = conn.execute(
        """
        SELECT a.id FROM activations a JOIN snapshots s ON s.id = a.snapshot_id
        WHERE a.catalog_id = ? AND a.revoked_at IS NULL AND (? IS NULL OR s.media_type = ?)
        ORDER BY a.id DESC LIMIT ?
        """,
        (catalog_id, media_type, media_type, runs),
    ).fetchall()
    return [r["id"] for r in rows]
