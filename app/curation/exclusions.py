"""
Exclusion names -> TMDB ids, picked automatically (PLAN.md section 6).

A catalog definition names what it must never show: ``blacklist_titles`` ("Title" or
"Title (YYYY)") and ``franchise_denylist`` (collection, company and keyword names, plus
title regexes). When a definition is applied, these names are resolved to TMDB ids here,
with no review step:

- **Titles**: TMDB search, then only *exact* normalised matches of ``title`` or
  ``original_title`` (no fuzzy matches: a near-miss could block an unrelated film).
  With a year, the exact match within +/- 1 year with the most votes. Without a year,
  every exact match (up to ``MAX_TITLE_PICKS``, most votes first): a bare title on a
  blacklist means every film of that name, so remakes are blocked too. Over-blocking a
  same-title film costs one candidate; under-blocking would serve an excluded film.
- **Collections / companies / keywords**: names equal to the query (ignoring a trailing
  "collection"); if there are none, every name containing the query as whole words (up to
  ``MAX_NAMED_PICKS``). "Some Saga" blocks "Some Saga Collection" and "Some Saga: Origins
  Collection", never "Awesome Saga".
- **Title patterns** are regexes, copied through.

A name with no match is listed in ``unresolved`` (shown on the status page); it blocks
nothing. ``scripts/resolve_blacklist.py`` shows the same candidates for inspection only.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from rapidfuzz import fuzz

from app.curation.tmdb_gate import (
    SIMILARITY_THRESHOLD,
    FranchiseDenylist,
    TMDBClient,
    clean_title,
    normalize_title,
    parse_year,
    split_title_year,
)

# franchise_denylist key -> (TMDB search endpoint, resolved id key).
NAMED_SECTIONS = {
    "collections": ("collection", "collection_ids"),
    "companies": ("company", "company_ids"),
    "keywords": ("keyword", "keyword_ids"),
}
MAX_TITLE_PICKS = 6
MAX_NAMED_PICKS = 8
_COLLECTION_SUFFIX_RE = re.compile(r"\s+collection$")


@dataclass(frozen=True)
class TitleQuery:
    title: str
    year: int | None = None

    @classmethod
    def parse(cls, value: Any) -> "TitleQuery":
        """``"Title"``, ``"Title (1999)"`` or ``{"title": ..., "year": ...}``."""
        if isinstance(value, Mapping):
            title, year = clean_title(str(value.get("title") or "")), value.get("year")
            if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
                raise ValueError(f"year of {title!r} must be a number")
        elif isinstance(value, str):
            title, year = split_title_year(clean_title(value), None)
        else:
            raise ValueError("each blacklist title must be text or {title, year}")
        if not normalize_title(title):
            raise ValueError("blacklist title is empty")
        return cls(title, year)

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


# --------------------------------------------------------------------------- matching


def title_candidates(query: TitleQuery, results: list[dict]) -> list[dict]:
    """Exact normalised matches by votes; fuzzy (>= 0.9) ones only when there is no exact match."""
    norm = normalize_title(query.title)
    exact, fuzzy, seen = [], [], set()
    for r in results:
        if not isinstance(r.get("id"), int) or r["id"] in seen:
            continue
        seen.add(r["id"])
        year = parse_year(r.get("release_date"))
        if query.year is not None and (year is None or abs(year - query.year) > 1):
            continue
        names = [normalize_title(n) for n in (r.get("title"), r.get("original_title")) if n]
        entry = {
            "id": r["id"],
            "title": r.get("title") or "",
            "year": year,
            "vote_count": int(r.get("vote_count") or 0),
        }
        if norm in names:
            exact.append({**entry, "fuzzy": False})
        elif any(fuzz.ratio(n, norm) >= SIMILARITY_THRESHOLD for n in names):
            fuzzy.append({**entry, "fuzzy": True})
    chosen = exact or fuzzy
    chosen.sort(key=lambda c: (-c["vote_count"], c["id"]))
    return chosen


def named_candidates(query: str, results: list[dict]) -> list[dict]:
    """Names containing the query as whole words ('Thor' matches 'Thor Collection', not 'Thorn')."""
    pattern = re.compile(r"\b" + re.escape(normalize_title(query)) + r"\b")
    out = []
    for r in results:
        if isinstance(r.get("id"), int) and pattern.search(normalize_title(r.get("name") or "")):
            out.append({"id": r["id"], "name": r.get("name") or ""})
    return out


def pick_titles(query: TitleQuery, results: list[dict]) -> list[int]:
    """The automatic pick for one blacklist title (module docstring)."""
    exact = [c for c in title_candidates(query, results) if not c["fuzzy"]]
    if query.year is not None:
        return [exact[0]["id"]] if exact else []
    return [c["id"] for c in exact[:MAX_TITLE_PICKS]]


def pick_named(query: str, results: list[dict]) -> list[int]:
    """The automatic pick for one collection / company / keyword name (module docstring)."""
    candidates = named_candidates(query, results)
    norm = _COLLECTION_SUFFIX_RE.sub("", normalize_title(query))
    equal = [c for c in candidates if _COLLECTION_SUFFIX_RE.sub("", normalize_title(c["name"])) == norm]
    return [c["id"] for c in (equal or candidates)[:MAX_NAMED_PICKS]]


async def search_titles(client: TMDBClient, query: TitleQuery) -> list[dict]:
    params = {"query": query.title, "include_adult": "false", "language": "en-US", "page": "1"}
    return (await client.get("/search/movie", params)).get("results") or []


async def search_named(client: TMDBClient, kind: str, query: str) -> list[dict]:
    return (await client.get(f"/search/{kind}", {"query": query, "page": "1"})).get("results") or []


# --------------------------------------------------------------------------- definition side


@dataclass(frozen=True)
class ExclusionInputs:
    """The exclusion names (and direct ids) of one definition."""

    titles: tuple[TitleQuery, ...] = ()
    collections: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()
    tmdb_ids: tuple[int, ...] = ()
    collection_ids: tuple[int, ...] = ()
    company_ids: tuple[int, ...] = ()
    keyword_ids: tuple[int, ...] = ()

    @property
    def needs_tmdb(self) -> bool:
        return bool(self.titles or self.collections or self.companies or self.keywords)

    @property
    def rev(self) -> str:
        """Hash of the names to resolve: resolution is redone only when they change."""
        doc = {
            "titles": [[t.title, t.year] for t in self.titles],
            "collections": list(self.collections),
            "companies": list(self.companies),
            "keywords": list(self.keywords),
        }
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode("utf-8")).hexdigest()


def _names(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of names")
    out = []
    for v in value:
        v = clean_title(v)
        if normalize_title(v) and v not in out:
            out.append(v)
    return tuple(out)


def _ids(value: Any, key: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        raise ValueError(f"{key} must be a list of TMDB ids (whole numbers)")
    return tuple(sorted(set(value)))


def exclusion_inputs(definition: Mapping[str, Any]) -> ExclusionInputs:
    """Parse ``blacklist_titles``, ``blacklist_tmdb_ids`` and ``franchise_denylist``. Raises ValueError."""
    titles_raw = definition.get("blacklist_titles") or []
    if not isinstance(titles_raw, list):
        raise ValueError("blacklist_titles must be a list")
    titles: list[TitleQuery] = []
    for value in titles_raw:
        q = TitleQuery.parse(value)
        if q not in titles:
            titles.append(q)
    deny = definition.get("franchise_denylist") or {}
    if isinstance(deny, list):  # a plain list means collection (franchise) names
        deny = {"collections": deny}
    if not isinstance(deny, Mapping):
        raise ValueError("franchise_denylist must be a list of franchise names or a mapping")
    allowed = {"collections", "companies", "keywords", "title_patterns", "collection_ids", "company_ids", "keyword_ids"}
    unknown = sorted(set(deny) - allowed)
    if unknown:
        raise ValueError(
            f"franchise_denylist: unknown key(s) {', '.join(unknown)}; expected {', '.join(sorted(allowed))}"
        )
    patterns = deny.get("title_patterns") or []
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError("franchise_denylist.title_patterns must be a list of regular expressions")
    for p in patterns:
        try:
            re.compile(p, re.IGNORECASE)
        except re.error:
            raise ValueError(f"franchise_denylist.title_patterns: invalid regular expression {p!r}") from None
    return ExclusionInputs(
        titles=tuple(titles),
        collections=_names(deny.get("collections"), "franchise_denylist.collections"),
        companies=_names(deny.get("companies"), "franchise_denylist.companies"),
        keywords=_names(deny.get("keywords"), "franchise_denylist.keywords"),
        title_patterns=tuple(patterns),
        tmdb_ids=_ids(definition.get("blacklist_tmdb_ids"), "blacklist_tmdb_ids"),
        collection_ids=_ids(deny.get("collection_ids"), "franchise_denylist.collection_ids"),
        company_ids=_ids(deny.get("company_ids"), "franchise_denylist.company_ids"),
        keyword_ids=_ids(deny.get("keyword_ids"), "franchise_denylist.keyword_ids"),
    )


@dataclass
class ResolvedExclusions:
    """What the gate enforces; stored as ``catalog_defs.exclusions_json``."""

    inputs_rev: str
    blacklist_tmdb_ids: list[int] = field(default_factory=list)
    collection_ids: list[int] = field(default_factory=list)
    company_ids: list[int] = field(default_factory=list)
    keyword_ids: list[int] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> "ResolvedExclusions | None":
        if not text:
            return None
        doc = json.loads(text)
        return cls(
            **{k: doc.get(k, []) for k in cls.__dataclass_fields__ if k != "inputs_rev"}, inputs_rev=doc["inputs_rev"]
        )

    def blacklist(self, inputs: ExclusionInputs) -> frozenset[int]:
        return frozenset(self.blacklist_tmdb_ids) | frozenset(inputs.tmdb_ids)

    def denylist(self, inputs: ExclusionInputs) -> FranchiseDenylist:
        return FranchiseDenylist(
            collection_ids=frozenset(self.collection_ids) | frozenset(inputs.collection_ids),
            company_ids=frozenset(self.company_ids) | frozenset(inputs.company_ids),
            keyword_ids=frozenset(self.keyword_ids) | frozenset(inputs.keyword_ids),
            title_patterns=inputs.title_patterns,
        )


def _sorted_unique(ids: Iterable[int]) -> list[int]:
    return sorted(set(ids))


async def resolve_exclusions(client: TMDBClient | None, inputs: ExclusionInputs) -> ResolvedExclusions:
    """
    Resolve every name with automatic picks. Raises ``TMDBError`` if TMDB fails (the caller
    then keeps the previously accepted definition). ``client`` may be None when nothing
    needs TMDB.
    """
    out = ResolvedExclusions(inputs_rev=inputs.rev)
    if not inputs.needs_tmdb:
        return out
    assert client is not None, "TMDB client required to resolve exclusion names"

    async def one_title(q: TitleQuery) -> tuple[str, list[int]]:
        return q.label, pick_titles(q, await search_titles(client, q))

    async def one_named(section: str, name: str) -> tuple[str, str, list[int]]:
        kind = NAMED_SECTIONS[section][0]
        return section, name, pick_named(name, await search_named(client, kind, name))

    titles = await asyncio.gather(*(one_title(q) for q in inputs.titles))
    named = await asyncio.gather(
        *(one_named(section, name) for section in NAMED_SECTIONS for name in getattr(inputs, section))
    )
    for label, ids in titles:
        out.blacklist_tmdb_ids += ids
        if not ids:
            out.unresolved.append(label)
    for section, name, ids in named:
        key = NAMED_SECTIONS[section][1]
        getattr(out, key).extend(ids)
        if not ids:
            out.unresolved.append(f"{section[:-1]}: {name}")
    out.blacklist_tmdb_ids = _sorted_unique(out.blacklist_tmdb_ids)
    out.collection_ids = _sorted_unique(out.collection_ids)
    out.company_ids = _sorted_unique(out.company_ids)
    out.keyword_ids = _sorted_unique(out.keyword_ids)
    return out
