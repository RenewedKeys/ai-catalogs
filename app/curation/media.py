"""
Movies or shows: the one switch a catalog definition makes with ``type`` (PLAN.md section 29).

``type: movie`` (the default) or ``type: series``. The Stremio type is used as is in the
manifest and on each meta; TMDB calls the same thing ``tv``. Movie and TV ids are separate
number spaces on TMDB, so nothing keyed by a TMDB id may mix the two.
"""

from dataclasses import dataclass
from typing import Any, Mapping

MOVIE = "movie"
SERIES = "series"
TYPES = (MOVIE, SERIES)

# Definition keys that have no meaning for shows: rejected when set on a series catalog and
# hidden in the editor. (Episode length is often missing on TMDB; TV has no collections; shows
# have no director. Watched history for shows is not read from MDBList yet.)
MOVIE_ONLY_KEYS = ("min_runtime", "max_per_collection", "director_cap", "exclude_watched")

# TMDB's genre lists (GET /genre/movie/list, GET /genre/tv/list). Reference data for names in
# the prompt and the editor's genre chips; an id missing here is rendered as "genre id N".
TMDB_MOVIE_GENRES: dict[int, str] = {
    28: "Action",
    12: "Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    14: "Fantasy",
    36: "History",
    27: "Horror",
    10402: "Music",
    9648: "Mystery",
    10749: "Romance",
    878: "Science Fiction",
    10770: "TV Movie",
    53: "Thriller",
    10752: "War",
    37: "Western",
}

TMDB_TV_GENRES: dict[int, str] = {
    10759: "Action & Adventure",
    16: "Animation",
    35: "Comedy",
    80: "Crime",
    99: "Documentary",
    18: "Drama",
    10751: "Family",
    10762: "Kids",
    9648: "Mystery",
    10763: "News",
    10764: "Reality",
    10765: "Sci-Fi & Fantasy",
    10766: "Soap",
    10767: "Talk",
    10768: "War & Politics",
    37: "Western",
}


@dataclass(frozen=True)
class Words:
    """Prompt and status wording for one type."""

    one: str  # "film" / "show"
    many: str  # "films" / "shows"
    released: str  # "Released" / "First aired"
    released_lower: str  # "released" / "first aired"


MOVIE_WORDS = Words("film", "films", "Released", "released")
SERIES_WORDS = Words("show", "shows", "First aired", "first aired")


def media_type(definition: Mapping[str, Any] | None) -> str:
    """The catalog's type; anything but ``series`` is a movie catalog (validation rejects the rest)."""
    return SERIES if (definition or {}).get("type") == SERIES else MOVIE


def tmdb_kind(kind: str) -> str:
    """TMDB's path segment for a type: ``movie`` or ``tv``."""
    return "tv" if kind == SERIES else "movie"


def words(kind: str) -> Words:
    return SERIES_WORDS if kind == SERIES else MOVIE_WORDS


def genres(kind: str) -> dict[int, str]:
    return TMDB_TV_GENRES if kind == SERIES else TMDB_MOVIE_GENRES
