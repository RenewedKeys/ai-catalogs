#!/usr/bin/env python3
"""
Resolve the seed catalog's blacklist titles and mega-franchises to TMDB ids, for review.

    TMDB_API_KEY=... python scripts/resolve_blacklist.py [--titles FILE] [--out FILE]

Output is YAML with two blocks, ``blacklist_tmdb_ids`` and ``franchise_denylist``. Every
plausible candidate is listed, one per line with a comment (title, year, votes), and
**nothing is chosen for you**: same-title films (The Thing 1982 / 2011, Total Recall
1990 / 2012, Virus, Fortress, Nemesis, ...) all appear, and you delete the lines you do not
want before copying the blocks into a catalog definition. Titles with no TMDB match are
listed as comments to resolve by hand.

The script never edits a catalog definition: it writes to stdout or ``--out``, and refuses
an ``--out`` inside ``catalogs.d/``.

``--titles FILE`` adds titles, one per line, optionally ``Title (YYYY)`` to narrow the
search; ``#`` starts a comment, and a line ``[group]`` sets the group for the lines after
it. Use it for the eight taste anchors of the master prompt, which PLAN.md does not name.

The TMDB key comes only from the ``TMDB_API_KEY`` environment variable.
"""

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from rapidfuzz import fuzz  # noqa: E402

from app.core.logging import describe_error  # noqa: E402
from app.curation.tmdb_gate import (  # noqa: E402
    SIMILARITY_THRESHOLD,
    TMDBClient,
    TMDBError,
    clean_title,
    normalize_title,
    parse_year,
    split_title_year,
)

# PLAN.md section 6, seed catalog table.
DEFAULT_TITLES: dict[str, list[str]] = {
    "taste_anchors": [],  # not named in PLAN.md: pass them with --titles
    "overused_defaults": [
        "Upgrade",
        "Dredd",
        "The Thing",
        "Total Recall",
        "Demolition Man",
        "Pitch Black",
        "Edge of Tomorrow",
        "Big Trouble in Little China",
        "They Live",
        "Escape from New York",
        "Escape from L.A.",
    ],
    "overused_action_scifi": [
        "Face/Off",
        "The Rock",
        "Broken Arrow",
        "Overlord",
        "Pandorum",
        "Equilibrium",
        "Lockout",
        "Deep Rising",
        "Dog Soldiers",
        "Slither",
        "Galaxy Quest",
        "Reign of Fire",
        "The Running Man",
        "Last Action Hero",
    ],
    "b_movie_filler": [
        "Hardware",
        "Cyborg",
        "Nemesis",
        "Split Second",
        "Fortress",
        "Leviathan",
        "DeepStar Six",
        "Virus",
        "Scanner Cop",
        "Project Shadowchaser",
        "Eliminators",
        "Steel Dawn",
        "Hell Comes to Frogtown",
        "Robot Jox",
    ],
}


@dataclass(frozen=True)
class Franchise:
    name: str
    collections: tuple[str, ...] = ()
    companies: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    title_patterns: tuple[str, ...] = ()


# Mega-franchises (PLAN.md section 6). Title patterns are suggestions: they are matched
# case-insensitively with re.search against title and original_title.
FRANCHISES: tuple[Franchise, ...] = (
    Franchise(
        "Marvel",
        collections=(
            "Avengers",
            "Iron Man",
            "Captain America",
            "Thor",
            "Guardians of the Galaxy",
            "X-Men",
            "Spider-Man",
        ),
        companies=("Marvel Studios", "Marvel Entertainment"),
        keywords=("marvel cinematic universe", "marvel comics", "superhero"),
        title_patterns=(r"\bavengers\b", r"\bx-men\b", r"\bspider-man\b"),
    ),
    Franchise(
        "DC",
        collections=("Batman", "Superman", "Wonder Woman", "Justice League", "The Dark Knight"),
        companies=("DC Films", "DC Studios", "DC Comics", "DC Entertainment"),
        keywords=("dc extended universe", "dc comics"),
        title_patterns=(r"\bbatman\b", r"\bsuperman\b", r"\bjustice league\b"),
    ),
    Franchise(
        "Star Wars",
        collections=("Star Wars",),
        companies=("Lucasfilm",),
        title_patterns=(r"\bstar wars\b",),
    ),
    Franchise("Star Trek", collections=("Star Trek",), title_patterns=(r"\bstar trek\b",)),
    Franchise(
        "Fast & Furious",
        collections=("Fast & Furious", "The Fast and the Furious"),
        title_patterns=(r"\bfast (&|and) (the )?furious\b", r"\bfurious [0-9]+\b", r"^fast five$", r"^fast x\b"),
    ),
    Franchise(
        "Jurassic Park",
        collections=("Jurassic Park", "Jurassic World"),
        title_patterns=(r"\bjurassic (park|world)\b",),
    ),
    Franchise("Transformers", collections=("Transformers",), title_patterns=(r"\btransformers\b",)),
    Franchise(
        "Alien",
        collections=("Alien", "Alien vs. Predator"),
        title_patterns=(r"^aliens?$", r"^alien(³|3| resurrection|: )", r"^aliens? vs\.? predator"),
    ),
    Franchise("Terminator", collections=("Terminator",), title_patterns=(r"\bterminator\b",)),
    Franchise(
        "Mission: Impossible",
        collections=("Mission: Impossible",),
        title_patterns=(r"\bmission:? impossible\b",),
    ),
    Franchise("John Wick", collections=("John Wick",), title_patterns=(r"\bjohn wick\b",)),
    Franchise("Mad Max", collections=("Mad Max",), title_patterns=(r"\bmad max\b",)),
)

MAX_TITLE_CANDIDATES = 6
MAX_NAMED_CANDIDATES = 8


@dataclass
class TitleQuery:
    group: str
    title: str
    year: int | None = None


@dataclass
class TitleResult:
    query: TitleQuery
    candidates: list[dict] = field(default_factory=list)  # {id, title, year, vote_count, fuzzy}


@dataclass
class NamedResult:
    query: str
    candidates: list[dict] = field(default_factory=list)  # {id, name}


@dataclass
class FranchiseResult:
    franchise: Franchise
    collections: list[NamedResult] = field(default_factory=list)
    companies: list[NamedResult] = field(default_factory=list)
    keywords: list[NamedResult] = field(default_factory=list)


def parse_titles_file(text: str, default_group: str = "taste_anchors") -> list[TitleQuery]:
    group = default_group
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip() or default_group
            continue
        title, year = split_title_year(clean_title(line), None)
        out.append(TitleQuery(group, title, year))
    return out


def default_queries() -> list[TitleQuery]:
    return [TitleQuery(group, t) for group, titles in DEFAULT_TITLES.items() for t in titles]


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
    return chosen[:MAX_TITLE_CANDIDATES]


def named_candidates(query: str, results: list[dict]) -> list[dict]:
    """Collections, companies or keywords whose normalised name contains the query."""
    norm = normalize_title(query)
    out = []
    for r in results:
        if isinstance(r.get("id"), int) and norm in normalize_title(r.get("name") or ""):
            out.append({"id": r["id"], "name": r.get("name") or ""})
    return out[:MAX_NAMED_CANDIDATES]


async def resolve_title(client: TMDBClient, query: TitleQuery) -> TitleResult:
    params = {"query": query.title, "include_adult": "false", "language": "en-US", "page": "1"}
    results = (await client.get("/search/movie", params)).get("results") or []
    return TitleResult(query, title_candidates(query, results))


async def resolve_named(client: TMDBClient, kind: str, query: str) -> NamedResult:
    results = (await client.get(f"/search/{kind}", {"query": query, "page": "1"})).get("results") or []
    return NamedResult(query, named_candidates(query, results))


async def resolve_franchise(client: TMDBClient, franchise: Franchise) -> FranchiseResult:
    collections, companies, keywords = await asyncio.gather(
        asyncio.gather(*(resolve_named(client, "collection", q) for q in franchise.collections)),
        asyncio.gather(*(resolve_named(client, "company", q) for q in franchise.companies)),
        asyncio.gather(*(resolve_named(client, "keyword", q) for q in franchise.keywords)),
    )
    return FranchiseResult(franchise, list(collections), list(companies), list(keywords))


async def resolve_all(
    client: TMDBClient, queries: list[TitleQuery], franchises=FRANCHISES
) -> tuple[list[TitleResult], list[FranchiseResult]]:
    titles = await asyncio.gather(*(resolve_title(client, q) for q in queries))
    found = await asyncio.gather(*(resolve_franchise(client, f) for f in franchises))
    return list(titles), list(found)


def _comment(text: str) -> str:
    return " ".join(str(text).split())


def _sq(text: str) -> str:
    """YAML single-quoted scalar (backslashes stay literal, quotes are doubled)."""
    return "'" + text.replace("'", "''") + "'"


def render_yaml(titles: list[TitleResult], franchises: list[FranchiseResult], today: date | None = None) -> str:
    today = today or date.today()
    lines = [
        f"# Generated by scripts/resolve_blacklist.py on {today.isoformat()}.",
        "# REVIEW BEFORE USE: every plausible candidate is listed and none is chosen for you.",
        "# Delete the lines you do not want, then copy the blocks into the catalog definition.",
        "# The app never reads this file.",
        "",
        "blacklist_tmdb_ids:",
    ]
    group = None
    for result in titles:
        q = result.query
        if q.group != group:
            group = q.group
            lines.append(f"  # ---- {group}")
        label = f"{q.title} ({q.year})" if q.year else q.title
        if not result.candidates:
            lines.append(f"  # {_comment(label)}: no TMDB match, resolve by hand")
            continue
        lines.append(f"  # {_comment(label)}")
        for c in result.candidates:
            note = f"{c['title']} ({c['year'] or 'no date'}), {c['vote_count']} votes"
            if c["fuzzy"]:
                note += ", fuzzy title match"
            lines.append(f"  - {c['id']}  # {_comment(note)}")
    if not any(r.query.group == "taste_anchors" for r in titles):
        lines.append("  # ---- taste_anchors: none given (PLAN.md does not name them); pass --titles")

    lines += ["", "franchise_denylist:"]
    for key, attr in (("collection_ids", "collections"), ("company_ids", "companies"), ("keyword_ids", "keywords")):
        lines.append(f"  {key}:")
        for fr in franchises:
            named = getattr(fr, attr)
            if not named:
                continue
            lines.append(f"    # ---- {_comment(fr.franchise.name)}")
            for n in named:
                if not n.candidates:
                    lines.append(f"    # {_comment(n.query)}: no match")
                for c in n.candidates:
                    lines.append(f"    - {c['id']}  # {_comment(c['name'])} (query: {_comment(n.query)})")
    lines.append("  title_patterns:")
    for fr in franchises:
        if fr.franchise.title_patterns:
            lines.append(f"    # ---- {_comment(fr.franchise.name)}")
            lines.extend(f"    - {_sq(p)}" for p in fr.franchise.title_patterns)
    return "\n".join(lines) + "\n"


def check_out_path(path: str) -> Path:
    out = Path(path).resolve()
    catalogs = (REPO / "catalogs.d").resolve()
    if out == catalogs or catalogs in out.parents:
        raise SystemExit("refusing to write inside catalogs.d/: this script never edits a catalog definition")
    return out


async def run(args: argparse.Namespace, transport=None) -> int:
    out = check_out_path(args.out) if args.out else None
    queries = [] if args.no_defaults else default_queries()
    if args.titles:
        queries = parse_titles_file(Path(args.titles).read_text(encoding="utf-8")) + queries
    try:
        async with TMDBClient(os.environ.get("TMDB_API_KEY"), transport=transport) as client:
            titles, franchises = await resolve_all(client, queries, () if args.no_franchises else FRANCHISES)
    except TMDBError as e:
        print(f"TMDB request failed: {describe_error(e)}", file=sys.stderr)
        return 1
    text = render_yaml(titles, franchises)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--titles", help="extra titles, one per line ('Title' or 'Title (YYYY)'; '[group]' headers)")
    parser.add_argument("--out", help="write YAML here instead of stdout (never inside catalogs.d/)")
    parser.add_argument("--no-defaults", action="store_true", help="skip the PLAN.md title lists")
    parser.add_argument("--no-franchises", action="store_true", help="skip franchise resolution")
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
