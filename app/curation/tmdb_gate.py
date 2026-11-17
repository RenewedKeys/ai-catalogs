"""
TMDB resolver and gatekeeper for curated catalogs (PLAN.md section 8).

``TMDBGate.check(title, year)`` turns one LLM suggestion into either an accepted
``Candidate`` or a ``Rejection`` with a reason code. Two steps:

1. **Resolve** the title to a TMDB id. Search with ``primary_release_year``; if no
   candidate is acceptable, search the adjacent years, then without a year. A candidate is
   acceptable when its normalised ``title`` or ``original_title`` equals the normalised
   query or scores >= 0.9 with ``rapidfuzz.fuzz.ratio`` (plain ratio: partial and token-set
   scorers would accept "Modern Times" for "Modern"), **and** its release year is within
   +/- 1 of the suggested year. Among acceptable candidates the highest ``vote_count``
   wins. Never ``results[0]``.
2. **Gatekeep** on the movie details (``evaluate()``, a pure function): runtime, rating,
   votes, language, genres, year range, adult, status, IMDb id, blacklist, franchise
   denylist. Thresholds are a ``GateRules`` value; milestone 5 builds it from catalog YAML.

HTTP: one shared ``httpx.AsyncClient``, at most ``concurrency`` requests in flight, retry on
429 (honouring ``Retry-After``) and on 5xx / timeouts / transport errors with backoff.
Errors that survive the retries raise ``TMDBError`` carrying a controlled category and the
HTTP status only; provider message text and URLs are never logged or stored.

Cache: the ``tmdb_cache`` table. Resolutions live 30 days (``no_match`` 7 days, so a film
added to TMDB later is picked up), details 7 days so ratings and vote counts stay fresh.
Errors are never cached. SQLite calls run in a thread, each on its own connection, and
writes happen only inside ``app.store.db.transaction()``.
"""

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable

import httpx
from rapidfuzz import fuzz

from app.core.config import settings
from app.core.logging import describe_error
from app.store import db

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"

DAY_MS = 24 * 60 * 60 * 1000
RESOLUTION_TTL_MS = 30 * DAY_MS
NO_MATCH_TTL_MS = 7 * DAY_MS
DETAILS_TTL_MS = 7 * DAY_MS

SIMILARITY_THRESHOLD = 90.0  # rapidfuzz scores are 0..100
YEAR_TOLERANCE = 1
MAX_TITLE_LENGTH = 200

DEFAULT_CONCURRENCY = 8
DEFAULT_TIMEOUT_S = 10.0
MAX_ATTEMPTS = 4
BACKOFF_S = (0.5, 1.0, 2.0)
MAX_RETRY_AFTER_S = 30.0

IMDB_ID_RE = re.compile(r"^tt\d+$")
_TRAILING_YEAR_RE = re.compile(r"\s*\((\d{4})\)\s*$")


class Reason(str, Enum):
    """Why a suggestion was rejected. Stored in ``runs.reject_reasons_json`` as counts."""

    NO_MATCH = "no_match"
    NOT_FOUND = "not_found"  # resolved id whose details 404
    RUNTIME_MISSING = "runtime_missing"
    RUNTIME_SHORT = "runtime_short"
    RATING_LOW = "rating_low"
    VOTES_LOW = "votes_low"
    LANGUAGE = "language"
    GENRE_EXCLUDED = "genre_excluded"
    YEAR_MISSING = "year_missing"
    YEAR_OUT_OF_RANGE = "year_out_of_range"
    ADULT = "adult"
    NOT_RELEASED = "not_released"
    IMDB_ID_MISSING = "imdb_id_missing"
    BLACKLISTED = "blacklisted"
    FRANCHISE_COLLECTION = "franchise_collection"
    FRANCHISE_COMPANY = "franchise_company"
    FRANCHISE_KEYWORD = "franchise_keyword"
    FRANCHISE_TITLE = "franchise_title"


class TMDBError(Exception):
    """
    A TMDB request that failed for good. ``category`` is a controlled value
    (``provider_auth``, ``provider_rate_limit``, ``provider_http_5xx``, ``provider_http_4xx``,
    ``timeout``, ``transport``); ``status_code`` is the HTTP status if there was one. The
    message never contains provider text or the request URL.
    """

    def __init__(self, category: str, status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        super().__init__(category if status_code is None else f"{category} (HTTP {status_code})")


# --------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class FranchiseDenylist:
    collection_ids: frozenset[int] = frozenset()
    company_ids: frozenset[int] = frozenset()
    keyword_ids: frozenset[int] = frozenset()
    title_patterns: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "collection_ids", frozenset(self.collection_ids))
        object.__setattr__(self, "company_ids", frozenset(self.company_ids))
        object.__setattr__(self, "keyword_ids", frozenset(self.keyword_ids))
        object.__setattr__(self, "title_patterns", tuple(self.title_patterns))
        # Validate now so a bad pattern fails when the catalog is loaded, not mid-run.
        object.__setattr__(self, "_compiled", tuple(re.compile(p, re.IGNORECASE) for p in self.title_patterns))

    def title_matches(self, *titles: str | None) -> bool:
        return any(rx.search(t) for rx in self._compiled for t in titles if t)


@dataclass(frozen=True)
class GateRules:
    """Hard filters (PLAN.md section 8). Defaults are the seed catalog's values."""

    min_runtime: int = 70
    min_vote_average: float = 6.2
    min_vote_count: int = 1000
    languages: frozenset[str] = frozenset({"en"})
    excluded_genre_ids: frozenset[int] = frozenset({16, 10770, 10751, 99})
    year_min: int | None = 1980
    year_max: int | None = None  # None = the current year
    blacklist_tmdb_ids: frozenset[int] = frozenset()
    franchise_denylist: FranchiseDenylist = field(default_factory=FranchiseDenylist)
    include_credits: bool = False  # director cap needs credits (one more cached call)

    def __post_init__(self):
        object.__setattr__(self, "languages", frozenset(self.languages))
        object.__setattr__(self, "excluded_genre_ids", frozenset(self.excluded_genre_ids))
        object.__setattr__(self, "blacklist_tmdb_ids", frozenset(self.blacklist_tmdb_ids))


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class Candidate:
    """An accepted movie, with what the pipeline needs for caps, quotas and metas."""

    tmdb_id: int
    imdb_id: str
    title: str
    original_title: str | None
    year: int
    runtime: int
    vote_average: float
    vote_count: int
    genre_ids: tuple[int, ...]
    collection_id: int | None
    director_ids: tuple[int, ...] | None  # None when credits were not requested
    poster_path: str | None
    overview: str | None


@dataclass(frozen=True)
class Rejection:
    reason: Reason
    title: str
    year: int | None
    tmdb_id: int | None = None
    reasons: tuple[Reason, ...] = ()  # every failed rule, ``reason`` is the first


@dataclass(frozen=True)
class SearchHit:
    tmdb_id: int
    title: str
    original_title: str | None
    year: int | None
    vote_count: int


# --------------------------------------------------------------------------- matching


def clean_title(title: str) -> str:
    """Strip control characters and cap the length before a title is used in a query."""
    title = "".join(ch for ch in title if unicodedata.category(ch)[0] != "C")
    return " ".join(title.split())[:MAX_TITLE_LENGTH]


def split_title_year(title: str, year: int | None) -> tuple[str, int | None]:
    """'Modern (2014)' -> ('Modern', 2014). An explicit ``year`` wins over the suffix."""
    m = _TRAILING_YEAR_RE.search(title)
    if m and title[: m.start()].strip():
        return title[: m.start()].strip(), year if year is not None else int(m.group(1))
    return title, year


def normalize_title(title: str) -> str:
    """Accent-, case- and punctuation-insensitive form: 'Mission: Impossible' == 'mission impossible'."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("&", " and ")
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def parse_year(release_date: Any) -> int | None:
    if isinstance(release_date, str) and len(release_date) >= 4 and release_date[:4].isdigit():
        return int(release_date[:4])
    return None


def title_matches(query_norm: str, hit: SearchHit) -> bool:
    for candidate in (hit.title, hit.original_title):
        if not candidate:
            continue
        norm = normalize_title(candidate)
        if norm == query_norm or fuzz.ratio(norm, query_norm) >= SIMILARITY_THRESHOLD:
            return True
    return False


def is_acceptable(query_norm: str, year: int | None, hit: SearchHit) -> bool:
    if not title_matches(query_norm, hit):
        return False
    if year is None:
        return True
    return hit.year is not None and abs(hit.year - year) <= YEAR_TOLERANCE


def pick_best(query_norm: str, year: int | None, hits: Iterable[SearchHit]) -> SearchHit | None:
    """The acceptable hit with the most votes (ties: lowest id, for determinism)."""
    acceptable = [h for h in hits if is_acceptable(query_norm, year, h)]
    if not acceptable:
        return None
    return max(acceptable, key=lambda h: (h.vote_count, -h.tmdb_id))


def search_plan(year: int | None) -> list[int | None]:
    """Year filters to try in order: the year, the adjacent years, then no year."""
    if year is None:
        return [None]
    return [year, year - 1, year + 1, None]


def _hits(payload: dict) -> list[SearchHit]:
    hits = []
    for r in payload.get("results") or []:
        if not isinstance(r, dict) or not isinstance(r.get("id"), int):
            continue
        hits.append(
            SearchHit(
                tmdb_id=r["id"],
                title=r.get("title") or "",
                original_title=r.get("original_title"),
                year=parse_year(r.get("release_date")),
                vote_count=int(r.get("vote_count") or 0),
            )
        )
    return hits


# --------------------------------------------------------------------------- gatekeeper


def _keyword_ids(details: dict) -> set[int]:
    # Movie details nest keywords as {"keywords": {"keywords": [{"id", "name"}]}}.
    block = details.get("keywords") or {}
    items = block.get("keywords") if isinstance(block, dict) else block
    return {k["id"] for k in items or [] if isinstance(k, dict) and isinstance(k.get("id"), int)}


def _imdb_id(details: dict) -> str | None:
    imdb = (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id")
    return imdb if isinstance(imdb, str) and IMDB_ID_RE.match(imdb) else None


def _director_ids(details: dict) -> tuple[int, ...] | None:
    credits = details.get("credits")
    if not isinstance(credits, dict):
        return None
    return tuple(
        sorted(
            {c["id"] for c in credits.get("crew") or [] if c.get("job") == "Director" and isinstance(c.get("id"), int)}
        )
    )


def evaluate(details: dict, rules: GateRules, today: date | None = None) -> list[Reason]:
    """Every hard rule ``details`` fails, in a fixed order. Empty means accepted."""
    reasons: list[Reason] = []
    tmdb_id = details.get("id")

    runtime = details.get("runtime")
    if not isinstance(runtime, int) or runtime <= 0:
        reasons.append(Reason.RUNTIME_MISSING)
    elif runtime < rules.min_runtime:
        reasons.append(Reason.RUNTIME_SHORT)

    if float(details.get("vote_average") or 0) < rules.min_vote_average:
        reasons.append(Reason.RATING_LOW)
    if int(details.get("vote_count") or 0) < rules.min_vote_count:
        reasons.append(Reason.VOTES_LOW)

    if details.get("original_language") not in rules.languages:
        reasons.append(Reason.LANGUAGE)

    genre_ids = {g.get("id") for g in details.get("genres") or [] if isinstance(g, dict)}
    if genre_ids & rules.excluded_genre_ids:
        reasons.append(Reason.GENRE_EXCLUDED)

    year = parse_year(details.get("release_date"))
    year_max = rules.year_max if rules.year_max is not None else (today or date.today()).year
    if year is None:
        reasons.append(Reason.YEAR_MISSING)
    elif (rules.year_min is not None and year < rules.year_min) or year > year_max:
        reasons.append(Reason.YEAR_OUT_OF_RANGE)

    if details.get("adult") is not False:
        reasons.append(Reason.ADULT)
    if details.get("status") != "Released":
        reasons.append(Reason.NOT_RELEASED)
    if _imdb_id(details) is None:
        reasons.append(Reason.IMDB_ID_MISSING)
    if tmdb_id in rules.blacklist_tmdb_ids:
        reasons.append(Reason.BLACKLISTED)

    deny = rules.franchise_denylist
    collection = details.get("belongs_to_collection")
    if isinstance(collection, dict) and collection.get("id") in deny.collection_ids:
        reasons.append(Reason.FRANCHISE_COLLECTION)
    companies = {c.get("id") for c in details.get("production_companies") or [] if isinstance(c, dict)}
    if companies & deny.company_ids:
        reasons.append(Reason.FRANCHISE_COMPANY)
    if _keyword_ids(details) & deny.keyword_ids:
        reasons.append(Reason.FRANCHISE_KEYWORD)
    if deny.title_matches(details.get("title"), details.get("original_title")):
        reasons.append(Reason.FRANCHISE_TITLE)
    return reasons


def to_candidate(details: dict) -> Candidate:
    collection = details.get("belongs_to_collection")
    return Candidate(
        tmdb_id=details["id"],
        imdb_id=_imdb_id(details) or "",
        title=details.get("title") or "",
        original_title=details.get("original_title"),
        year=parse_year(details.get("release_date")) or 0,
        runtime=int(details.get("runtime") or 0),
        vote_average=float(details.get("vote_average") or 0),
        vote_count=int(details.get("vote_count") or 0),
        genre_ids=tuple(g["id"] for g in details.get("genres") or [] if isinstance(g, dict) and "id" in g),
        collection_id=collection.get("id") if isinstance(collection, dict) else None,
        director_ids=_director_ids(details),
        poster_path=details.get("poster_path"),
        overview=details.get("overview"),
    )


# --------------------------------------------------------------------------- cache


class TMDBCache:
    """``tmdb_cache`` rows. Synchronous; ``TMDBGate`` calls it through ``asyncio.to_thread``."""

    def __init__(self, path: str | None = None):
        self.path = path

    def get(self, key: str, ttl_ms: int) -> tuple[int | None, str | None] | None:
        with db.session(self.path) as conn:
            row = conn.execute(
                "SELECT tmdb_id, details_json, fetched_at FROM tmdb_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None or db.now_ms() - row["fetched_at"] >= ttl_ms:
            return None
        return row["tmdb_id"], row["details_json"]

    def put(self, key: str, tmdb_id: int | None, details_json: str | None) -> None:
        with db.session(self.path) as conn, db.transaction(conn):
            conn.execute(
                "INSERT INTO tmdb_cache (key, tmdb_id, details_json, fetched_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET tmdb_id = excluded.tmdb_id, "
                "details_json = excluded.details_json, fetched_at = excluded.fetched_at",
                (key, tmdb_id, details_json, db.now_ms()),
            )


_NO_MATCH = "no_match"


def resolution_key(norm_title: str, year: int | None) -> str:
    return f"resolve:{norm_title}:{year if year is not None else '-'}"


def details_key(tmdb_id: int, credits: bool) -> str:
    return f"details:{tmdb_id}:{'credits' if credits else 'base'}"


# --------------------------------------------------------------------------- client


def _retry_after_s(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date or garbage: fall back to backoff
    if seconds != seconds or seconds < 0:  # NaN
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


class TMDBClient:
    """
    Thin async TMDB v3 client: one shared ``httpx.AsyncClient``, a concurrency limit and
    the retry policy. ``get()`` returns parsed JSON or raises ``TMDBError``; a 404 raises
    ``TMDBError("not_found", 404)``.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        base_url: str = TMDB_BASE_URL,
    ):
        token = token or settings.TMDB_READ_ACCESS_TOKEN
        if not token:
            raise TMDBError("provider_auth")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            transport=transport,
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self.requests = 0  # attempts sent, for stats and tests

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TMDBClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict:
        last: TMDBError | None = None
        for attempt in range(MAX_ATTEMPTS):
            wait: float | None = None
            try:
                async with self._semaphore:
                    self.requests += 1
                    response = await self._client.get(path, params=params)
            except httpx.TimeoutException:
                last = TMDBError("timeout")
            except httpx.TransportError:
                last = TMDBError("transport")
            else:
                status = response.status_code
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        last = TMDBError("provider_http_5xx", status)  # truncated body: retry
                elif status == 429:
                    last = TMDBError("provider_rate_limit", status)
                    wait = _retry_after_s(response)
                elif status >= 500:
                    last = TMDBError("provider_http_5xx", status)
                elif status in (401, 403):
                    raise TMDBError("provider_auth", status)
                elif status == 404:
                    raise TMDBError("not_found", status)
                else:
                    raise TMDBError("provider_http_4xx", status)
            if attempt + 1 < MAX_ATTEMPTS:
                logger.info("TMDB request retry %d: %s", attempt + 1, describe_error(last))
                await self._sleep(wait if wait is not None else BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        logger.warning("TMDB request failed after %d attempts: %s", MAX_ATTEMPTS, describe_error(last))
        raise last


# --------------------------------------------------------------------------- gate


class TMDBGate:
    """
    Resolve + gatekeep, with caching. Use as an async context manager::

        async with TMDBGate(rules) as gate:
            result = await gate.check("The Thing", 1982)

    ``check`` returns a ``Candidate`` or a ``Rejection``; it raises ``TMDBError`` when TMDB
    itself fails (auth, exhausted retries), which the pipeline classifies as a run error.
    """

    def __init__(self, rules: GateRules | None = None, *, client: TMDBClient | None = None, db_path: str | None = None):
        self.rules = rules or GateRules()
        self._client = client
        self._owns_client = client is None
        self.cache = TMDBCache(db_path)

    @property
    def client(self) -> TMDBClient:
        if self._client is None:
            self._client = TMDBClient()
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def __aenter__(self) -> "TMDBGate":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def resolve(self, title: str, year: int | None) -> int | None:
        """TMDB id for an LLM suggestion, or None (``no_match``)."""
        title, year = split_title_year(clean_title(title), year)
        query_norm = normalize_title(title)
        if not query_norm:
            return None
        key = resolution_key(query_norm, year)
        cached = await asyncio.to_thread(self.cache.get, key, RESOLUTION_TTL_MS)
        if cached is not None:
            if cached[1] != _NO_MATCH:
                return cached[0]
            negative = await asyncio.to_thread(self.cache.get, key, NO_MATCH_TTL_MS)
            if negative is not None:
                return None

        best: SearchHit | None = None
        for year_filter in search_plan(year):
            params = {"query": title, "include_adult": "false", "language": "en-US", "page": "1"}
            if year_filter is not None:
                params["primary_release_year"] = str(year_filter)
            best = pick_best(query_norm, year, _hits(await self.client.get("/search/movie", params)))
            if best is not None:
                break

        tmdb_id = best.tmdb_id if best else None
        await asyncio.to_thread(self.cache.put, key, tmdb_id, None if best else _NO_MATCH)
        return tmdb_id

    async def details(self, tmdb_id: int) -> dict | None:
        """Movie details with external ids and keywords (and credits if the rules need them)."""
        credits = self.rules.include_credits
        key = details_key(tmdb_id, credits)
        cached = await asyncio.to_thread(self.cache.get, key, DETAILS_TTL_MS)
        if cached is not None and cached[1]:
            return json.loads(cached[1])
        append = "external_ids,keywords" + (",credits" if credits else "")
        try:
            details = await self.client.get(f"/movie/{tmdb_id}", {"language": "en-US", "append_to_response": append})
        except TMDBError as e:
            if e.category == "not_found":
                return None
            raise
        if credits:
            # Only the directors are needed; full cast lists would bloat the cache.
            crew = (details.get("credits") or {}).get("crew") or []
            details["credits"] = {"crew": [c for c in crew if isinstance(c, dict) and c.get("job") == "Director"]}
        await asyncio.to_thread(self.cache.put, key, tmdb_id, json.dumps(details, separators=(",", ":")))
        return details

    async def check(self, title: str, year: int | None) -> Candidate | Rejection:
        tmdb_id = await self.resolve(title, year)
        if tmdb_id is None:
            return Rejection(Reason.NO_MATCH, title, year, reasons=(Reason.NO_MATCH,))
        details = await self.details(tmdb_id)
        if details is None:
            return Rejection(Reason.NOT_FOUND, title, year, tmdb_id, (Reason.NOT_FOUND,))
        reasons = evaluate(details, self.rules)
        if reasons:
            return Rejection(reasons[0], title, year, tmdb_id, tuple(reasons))
        return to_candidate(details)
