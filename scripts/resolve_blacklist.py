#!/usr/bin/env python3
"""
Resolve any catalog's exclusion list (titles, franchises, studios, keywords) to TMDB ids.

    TMDB_API_KEY=... python scripts/resolve_blacklist.py EXCLUSIONS_FILE [--out FILE]

Generic: nothing about a particular catalog is built in. The input file lists what one
catalog should never show, in sections (``#`` starts a comment)::

    [titles]            # default section; "Title" or "Title (YYYY)" per line
    The Thing (1982)
    Total Recall
    [franchises]        # resolved to TMDB collections by name
    Mad Max
    [companies]         # production companies, e.g. Marvel Studios
    Lucasfilm
    [keywords]          # TMDB keywords, e.g. marvel cinematic universe
    dc extended universe
    [title_patterns]    # regexes, copied through unchanged
    \\bstar wars\\b

``docs/examples/cult-picks-exclusions.txt`` is the example catalog's list.

Output is YAML with ``blacklist_tmdb_ids`` and ``franchise_denylist`` blocks, every
plausible candidate on its own line with a comment (title, year, votes). Same-title films
are all listed (The Thing 1982 / 2011); a year in the input narrows to +/- 1.

The script never edits a catalog definition: it writes to stdout or ``--out``, and refuses
an ``--out`` inside ``catalogs.d/``. The TMDB key comes only from ``TMDB_API_KEY`` (a v4
read access token or a v3 API key).
"""

import argparse
import asyncio
import logging
import os
import re
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

SECTIONS = ("titles", "franchises", "companies", "keywords", "title_patterns")
# Input section -> (TMDB search endpoint, output key under franchise_denylist).
NAMED_SECTIONS = {
    "franchises": ("collection", "collection_ids"),
    "companies": ("company", "company_ids"),
    "keywords": ("keyword", "keyword_ids"),
}

MAX_TITLE_CANDIDATES = 6
MAX_NAMED_CANDIDATES = 8


class InputError(ValueError):
    pass


@dataclass
class TitleQuery:
    title: str
    year: int | None = None


@dataclass
class Exclusions:
    titles: list[TitleQuery] = field(default_factory=list)
    franchises: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    title_patterns: list[str] = field(default_factory=list)


@dataclass
class TitleResult:
    query: TitleQuery
    candidates: list[dict] = field(default_factory=list)  # {id, title, year, vote_count, fuzzy}


@dataclass
class NamedResult:
    section: str
    query: str
    candidates: list[dict] = field(default_factory=list)  # {id, name}


def parse_exclusions(text: str) -> Exclusions:
    out = Exclusions()
    section = "titles"
    for number, raw in enumerate(text.splitlines(), 1):
        # Patterns may contain '#', so only a '#' at the start of a pattern line is a comment.
        line = raw.strip() if section == "title_patterns" else raw.split("#", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in SECTIONS:
                raise InputError(f"line {number}: unknown section [{section}]; expected one of {', '.join(SECTIONS)}")
            continue
        if section == "titles":
            title, year = split_title_year(clean_title(line), None)
            out.titles.append(TitleQuery(title, year))
        elif section == "title_patterns":
            try:
                re.compile(line, re.IGNORECASE)
            except re.error:
                raise InputError(f"line {number}: invalid regular expression") from None
            out.title_patterns.append(line)
        else:
            getattr(out, section).append(clean_title(line))
    return out


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
    return chosen  # render_yaml shows the first MAX_TITLE_CANDIDATES


def named_candidates(query: str, results: list[dict]) -> list[dict]:
    """Names containing the query as whole words ('Thor' matches 'Thor Collection', not 'Thorn')."""
    pattern = re.compile(r"\b" + re.escape(normalize_title(query)) + r"\b")
    out = []
    for r in results:
        if isinstance(r.get("id"), int) and pattern.search(normalize_title(r.get("name") or "")):
            out.append({"id": r["id"], "name": r.get("name") or ""})
    return out  # render_yaml shows the first MAX_NAMED_CANDIDATES


async def resolve_title(client: TMDBClient, query: TitleQuery) -> TitleResult:
    params = {"query": query.title, "include_adult": "false", "language": "en-US", "page": "1"}
    results = (await client.get("/search/movie", params)).get("results") or []
    return TitleResult(query, title_candidates(query, results))


async def resolve_named(client: TMDBClient, section: str, query: str) -> NamedResult:
    kind = NAMED_SECTIONS[section][0]
    results = (await client.get(f"/search/{kind}", {"query": query, "page": "1"})).get("results") or []
    return NamedResult(section, query, named_candidates(query, results))


async def resolve_all(client: TMDBClient, ex: Exclusions) -> tuple[list[TitleResult], list[NamedResult]]:
    titles = await asyncio.gather(*(resolve_title(client, q) for q in ex.titles))
    named = await asyncio.gather(
        *(resolve_named(client, section, q) for section in NAMED_SECTIONS for q in getattr(ex, section))
    )
    return list(titles), list(named)


def _comment(text: str) -> str:
    return " ".join(str(text).split())


def _sq(text: str) -> str:
    """YAML single-quoted scalar (backslashes stay literal, quotes are doubled)."""
    return "'" + text.replace("'", "''") + "'"


def render_yaml(
    titles: list[TitleResult],
    named: list[NamedResult],
    title_patterns: list[str] = (),
    today: date | None = None,
) -> str:
    today = today or date.today()
    lines = [
        f"# Generated by scripts/resolve_blacklist.py on {today.isoformat()}.",
        "# Every plausible candidate is listed; remove any you do not want excluded.",
        "",
        "blacklist_tmdb_ids:",
    ]
    for result in titles:
        q = result.query
        label = f"{q.title} ({q.year})" if q.year else q.title
        if not result.candidates:
            lines.append(f"  # {_comment(label)}: no TMDB match")
            continue
        lines.append(f"  # {_comment(label)}")
        for c in result.candidates[:MAX_TITLE_CANDIDATES]:
            note = f"{c['title']} ({c['year'] or 'no date'}), {c['vote_count']} votes"
            if c["fuzzy"]:
                note += ", fuzzy title match"
            lines.append(f"  - {c['id']}  # {_comment(note)}")
        if len(result.candidates) > MAX_TITLE_CANDIDATES:
            lines.append(f"  # ... {len(result.candidates) - MAX_TITLE_CANDIDATES} more not shown (fewer votes)")

    lines += ["", "franchise_denylist:"]
    for section, (_, key) in NAMED_SECTIONS.items():
        lines.append(f"  {key}:")
        for n in (n for n in named if n.section == section):
            if not n.candidates:
                lines.append(f"    # {_comment(n.query)}: no match")
            for c in n.candidates[:MAX_NAMED_CANDIDATES]:
                lines.append(f"    - {c['id']}  # {_comment(c['name'])} (query: {_comment(n.query)})")
            if len(n.candidates) > MAX_NAMED_CANDIDATES:
                lines.append(f"    # ... {len(n.candidates) - MAX_NAMED_CANDIDATES} more not shown for this query")
    lines.append("  title_patterns:")
    lines.extend(f"    - {_sq(p)}" for p in title_patterns)
    return "\n".join(lines) + "\n"


def check_out_path(path: str) -> Path:
    out = Path(path).resolve()
    catalogs = (REPO / "catalogs.d").resolve()
    if out == catalogs or catalogs in out.parents:
        raise SystemExit("refusing to write inside catalogs.d/: this script never edits a catalog definition")
    return out


def _logs_to_stderr() -> None:
    """app.core.logging logs to stdout; stdout here is the YAML, so move every handler."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream in (sys.stdout, sys.__stdout__):
            handler.setStream(sys.stderr)


async def run(args: argparse.Namespace, transport=None) -> int:
    _logs_to_stderr()
    out = check_out_path(args.out) if args.out else None
    try:
        exclusions = parse_exclusions(Path(args.exclusions).read_text(encoding="utf-8"))
    except (OSError, InputError) as e:
        print(f"{args.exclusions}: {e}", file=sys.stderr)
        return 2
    if not os.environ.get("TMDB_API_KEY"):
        print("TMDB_API_KEY is not set", file=sys.stderr)
        return 1
    try:
        async with TMDBClient(os.environ["TMDB_API_KEY"], transport=transport) as client:
            titles, named = await resolve_all(client, exclusions)
    except TMDBError as e:
        print(f"TMDB request failed: {describe_error(e)}", file=sys.stderr)
        return 1
    text = render_yaml(titles, named, exclusions.title_patterns)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("exclusions", help="exclusion list: [titles] [franchises] [companies] [keywords] sections")
    parser.add_argument("--out", help="write YAML here instead of stdout (never inside catalogs.d/)")
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
