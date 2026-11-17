"""
The viewer's watched films, from MDBList (PLAN.md section 19).

A catalog with ``exclude_watched: true`` never publishes a film the owner has already
watched. The watched set comes from the owner's MDBList account (``GET /sync/watched``,
which MDBList fills from Trakt, Plex, Jellyfin and its own check-ins) and is matched on
``tmdb_id`` and ``imdb_id``.

- Key: ``MDBLIST_API_KEY`` from the environment only. MDBList takes it as the ``apikey``
  query parameter, so every request URL carries it: URLs and exception text are never
  logged or stored, only ``WatchedError`` categories and HTTP statuses.
- Fetch: ``mediatype=movie``, full payload (``extended=ids_only`` drops the IMDb id and the
  title), cursor pagination (``pagination.next_cursor``) at 1,000 rows per page.
- Cache: one row in ``watched_cache``, written only after a complete fetch, reused for
  ``WATCHED_TTL_MS`` (6 h) by every catalog and run. An expired cache whose refresh fails
  fails the run: a list is never published without the filter, and never with a stale one.
- Errors: ``WatchedError.category`` is one of ``mdblist_auth`` (401/403), ``mdblist_rate_limit``
  (429), ``mdblist_http_5xx``, ``mdblist_timeout``, ``mdblist_transport``,
  ``mdblist_http_4xx``, ``mdblist_schema`` (unexpected payload) and ``config`` (no key). The
  first four after the retries are transient: the job is retried later and the previous
  snapshot stays active.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional

import httpx

from app.core.config import settings
from app.core.logging import describe_error
from app.store import db

logger = logging.getLogger(__name__)

MDBLIST_BASE_URL = "https://api.mdblist.com"
WATCHED_PATH = "/sync/watched"
CACHE_KEY = "mdblist:movies"

HOUR_MS = 60 * 60 * 1000
WATCHED_TTL_MS = 6 * HOUR_MS
PAGE_LIMIT = 1000
MAX_PAGES = 200  # 200,000 watched films; more means the cursor is not advancing
MAX_PROMPT_TITLES = 1000  # titles kept in the cache for the prompt block (newest first)

DEFAULT_TIMEOUT_S = 20.0
MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 3.0)
MAX_RETRY_AFTER_S = 30.0  # the daily limit's Retry-After can be hours: give up, retry the job later

TRANSIENT = frozenset({"mdblist_rate_limit", "mdblist_http_5xx", "mdblist_timeout", "mdblist_transport"})


class WatchedError(Exception):
    """A watched-history fetch that failed. Category and HTTP status only; never provider text."""

    def __init__(self, category: str, status_code: Optional[int] = None):
        self.category = category
        self.status_code = status_code
        super().__init__(category if status_code is None else f"{category} (HTTP {status_code})")

    @property
    def transient(self) -> bool:
        return self.category in TRANSIENT


@dataclass(frozen=True)
class WatchedSet:
    tmdb_ids: frozenset[int]
    imdb_ids: frozenset[str]
    titles: tuple[tuple[str, Optional[int]], ...] = ()  # most recently watched first
    fetched_at: int = 0

    def __len__(self) -> int:
        return max(len(self.tmdb_ids), len(self.imdb_ids))

    def contains(self, tmdb_id: Optional[int] = None, imdb_id: Optional[str] = None) -> bool:
        return (tmdb_id is not None and tmdb_id in self.tmdb_ids) or (imdb_id is not None and imdb_id in self.imdb_ids)

    def to_json(self) -> str:
        return json.dumps(
            {
                "tmdb_ids": sorted(self.tmdb_ids),
                "imdb_ids": sorted(self.imdb_ids),
                "titles": [list(t) for t in self.titles],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str, fetched_at: int) -> Optional["WatchedSet"]:
        try:
            data = json.loads(text)
            return cls(
                tmdb_ids=frozenset(int(t) for t in data["tmdb_ids"]),
                imdb_ids=frozenset(str(i) for i in data["imdb_ids"]),
                titles=tuple((str(t), y if isinstance(y, int) else None) for t, y in data.get("titles", [])),
                fetched_at=fetched_at,
            )
        except (ValueError, KeyError, TypeError):
            return None  # unreadable cache row: refetch


# --------------------------------------------------------------------------- parsing


def _year(value: Any) -> Optional[int]:
    if isinstance(value, int) and 1800 <= value <= 3000:
        return value
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return _year(int(value[:4]))
    return None


def _row_ids(row: Any) -> tuple[Optional[int], Optional[str], str, Optional[int], str]:
    """(tmdb_id, imdb_id, title, year, watched_at) of one ``movies`` row; tolerant of both shapes."""
    if not isinstance(row, dict):
        return None, None, "", None, ""
    movie = row.get("movie") if isinstance(row.get("movie"), dict) else row
    ids = movie.get("ids") if isinstance(movie.get("ids"), dict) else movie
    tmdb = ids.get("tmdb")
    tmdb_id = tmdb if isinstance(tmdb, int) and not isinstance(tmdb, bool) and tmdb > 0 else None
    if tmdb_id is None and isinstance(tmdb, str) and tmdb.isdigit():
        tmdb_id = int(tmdb) or None
    imdb = ids.get("imdb")
    imdb_id = imdb.strip() if isinstance(imdb, str) and imdb.strip().startswith("tt") else None
    title = movie.get("title") if isinstance(movie.get("title"), str) else ""
    year = _year(movie.get("year")) or _year(movie.get("release_date"))
    watched_at = row.get("last_watched_at") or row.get("watched_at") or ""
    return tmdb_id, imdb_id, title.strip(), year, watched_at if isinstance(watched_at, str) else ""


def parse_rows(rows: Iterable[Any]) -> WatchedSet:
    tmdb_ids: set[int] = set()
    imdb_ids: set[str] = set()
    dated: list[tuple[str, str, Optional[int]]] = []
    for row in rows:
        tmdb_id, imdb_id, title, year, watched_at = _row_ids(row)
        if tmdb_id is None and imdb_id is None:
            continue  # no usable id
        if tmdb_id is not None:
            tmdb_ids.add(tmdb_id)
        if imdb_id is not None:
            imdb_ids.add(imdb_id)
        if title:
            dated.append((watched_at, title, year))
    dated.sort(key=lambda d: d[0], reverse=True)  # ISO 8601: text order is time order
    seen: set[tuple[str, Optional[int]]] = set()
    titles = []
    for _, title, year in dated:
        if (title.casefold(), year) not in seen:
            seen.add((title.casefold(), year))
            titles.append((title, year))
            if len(titles) >= MAX_PROMPT_TITLES:
                break
    return WatchedSet(frozenset(tmdb_ids), frozenset(imdb_ids), tuple(titles))


# --------------------------------------------------------------------------- client


def _retry_after_s(response: httpx.Response) -> Optional[float]:
    try:
        seconds = float(response.headers.get("Retry-After", "").strip())
    except ValueError:
        return None
    if seconds != seconds or seconds < 0:
        return None
    return seconds


class MDBListClient:
    """
    Read-only MDBList client for the watched history. Raises ``WatchedError("config")`` when
    no key is configured, so a catalog that asks for the filter fails with a clear reason.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        base_url: str = MDBLIST_BASE_URL,
    ):
        key = (api_key if api_key is not None else settings.MDBLIST_API_KEY or "").strip()
        if not key:
            raise WatchedError("config")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"accept": "application/json"},
            params={"apikey": key},  # the request URL carries the key: never log URLs
            timeout=timeout_s,
            transport=transport,
        )
        self._sleep = sleep
        self.requests = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict:
        last: Optional[WatchedError] = None
        for attempt in range(MAX_ATTEMPTS):
            wait: Optional[float] = None
            try:
                self.requests += 1
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException:
                last = WatchedError("mdblist_timeout")
            except httpx.TransportError:
                last = WatchedError("mdblist_transport")
            else:
                status = response.status_code
                if status == 200:
                    try:
                        body = response.json()
                    except ValueError:
                        last = WatchedError("mdblist_http_5xx", status)  # truncated body: retry
                    else:
                        if not isinstance(body, dict):
                            raise WatchedError("mdblist_schema", status)
                        return body
                elif status == 429:
                    last = WatchedError("mdblist_rate_limit", status)
                    wait = _retry_after_s(response)
                    if wait is not None and wait > MAX_RETRY_AFTER_S:
                        break  # daily limit: retrying now cannot succeed
                elif status >= 500:
                    last = WatchedError("mdblist_http_5xx", status)
                elif status in (401, 403):
                    raise WatchedError("mdblist_auth", status)
                else:
                    raise WatchedError("mdblist_http_4xx", status)
            if attempt + 1 < MAX_ATTEMPTS:
                logger.info("MDBList request retry %d: %s", attempt + 1, describe_error(last))
                await self._sleep(wait if wait is not None else BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        logger.warning("MDBList request failed: %s", describe_error(last))
        raise last

    async def watched_movies(self) -> WatchedSet:
        """Every watched movie, all pages. Raises ``WatchedError``; never returns a partial set."""
        rows: list[Any] = []
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"mediatype": "movie", "limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            body = await self._get(WATCHED_PATH, params)
            movies = body.get("movies")
            if not isinstance(movies, list):
                raise WatchedError("mdblist_schema")  # never read a shape change as "nothing watched"
            rows.extend(movies)
            pagination = body.get("pagination")
            cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
            if not cursor or not movies:
                return parse_rows(rows)
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise WatchedError("mdblist_schema")
            seen_cursors.add(cursor)
        raise WatchedError("mdblist_schema")  # the cursor never ran out


# --------------------------------------------------------------------------- cache


def read_cache(conn, now: Optional[int] = None, ttl_ms: int = WATCHED_TTL_MS) -> Optional[WatchedSet]:
    row = conn.execute("SELECT fetched_at, payload_json FROM watched_cache WHERE key = ?", (CACHE_KEY,)).fetchone()
    if row is None:
        return None
    now = db.now_ms() if now is None else now
    if now - row["fetched_at"] >= ttl_ms or row["fetched_at"] > now:
        return None
    return WatchedSet.from_json(row["payload_json"], row["fetched_at"])


def write_cache(conn, watched: WatchedSet, now: Optional[int] = None) -> None:
    now = db.now_ms() if now is None else now
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO watched_cache (key, fetched_at, item_count, payload_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, "
            "item_count = excluded.item_count, payload_json = excluded.payload_json",
            (CACHE_KEY, now, len(watched), watched.to_json()),
        )


async def load_watched(
    client_factory: Callable[[], Any],
    *,
    db_path: Optional[str] = None,
    ttl_ms: int = WATCHED_TTL_MS,
) -> WatchedSet:
    """
    The watched set for this run: the cached one while it is fresh, else a full fetch that
    replaces the cache. ``client_factory`` is called first, so a missing key is a ``config``
    error even while a cached set exists. Raises ``WatchedError``.
    """
    client = client_factory()
    try:

        def _read():
            with db.session(db_path) as conn:
                return read_cache(conn, ttl_ms=ttl_ms)

        cached = await asyncio.to_thread(_read)
        if cached is not None:
            return cached
        fresh = await client.watched_movies()
        now = db.now_ms()

        def _write():
            with db.session(db_path) as conn:
                write_cache(conn, fresh, now)

        await asyncio.to_thread(_write)
        return WatchedSet(fresh.tmdb_ids, fresh.imdb_ids, fresh.titles, now)
    finally:
        closer = getattr(client, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception as e:
                logger.warning("Closing the MDBList client failed: %s", describe_error(e))
