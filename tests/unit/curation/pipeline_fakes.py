"""
Fakes for pipeline, runner and CLI tests: a synthetic TMDB "universe" of neutral placeholder
films ("Film 1", "Film 2", ...) and a scripted LLM that suggests them. No real titles, no
network: the real ``CuratedLLMService`` and ``TMDBGate`` run against mock transports.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

import httpx
import httpx2

from app.curation.llm import CuratedLLMService, LLMSettings
from app.curation.runner import Services
from app.curation.tmdb_gate import TMDBClient, TMDBGate, normalize_title
from tests.unit.curation.llm_fake import BODY_SENTINEL, completion

IMDB_BASE = 1_000_000


@dataclass
class Film:
    n: int
    year: int
    collection_id: Optional[int] = None
    director_ids: tuple[int, ...] = ()
    vote_average: float = 7.0
    vote_count: int = 5000
    language: str = "en"
    genre_ids: tuple[int, ...] = (18,)
    runtime: int = 100
    category: str = "alpha"

    @property
    def title(self) -> str:
        return f"Film {self.n}"

    @property
    def imdb_id(self) -> str:
        return f"tt{IMDB_BASE + self.n}"

    def details(self, credits: bool) -> dict:
        d = {
            "id": self.n,
            "title": self.title,
            "original_title": self.title,
            "release_date": f"{self.year}-06-01",
            "runtime": self.runtime,
            "vote_average": self.vote_average,
            "vote_count": self.vote_count,
            "original_language": self.language,
            "genres": [{"id": g, "name": f"genre {g}"} for g in self.genre_ids],
            "adult": False,
            "status": "Released",
            "external_ids": {"imdb_id": self.imdb_id},
            "keywords": {"keywords": []},
            "production_companies": [],
            "belongs_to_collection": (
                {"id": self.collection_id, "name": f"Saga {self.collection_id}"} if self.collection_id else None
            ),
            "poster_path": f"/p{self.n}.jpg",
            "overview": f"Overview of film {self.n}.",
        }
        if credits:
            d["credits"] = {"crew": [{"id": i, "job": "Director", "name": f"Director {i}"} for i in self.director_ids]}
        return d


def default_year(n: int) -> int:
    return 1980 + n % 45  # 1980..2024


def make_universe(n: int, **overrides: Callable[[int], object]) -> dict[int, Film]:
    films = {}
    for i in range(1, n + 1):
        kwargs = {"year": default_year(i), "director_ids": (50_000 + i,)}
        kwargs.update({k: fn(i) for k, fn in overrides.items()})
        films[i] = Film(i, **kwargs)
    return films


_FILM_RE = re.compile(r"^film (\d+)$")


class UniverseTMDB:
    """``/search/movie``, ``/movie/{id}`` and the collection/company/keyword searches."""

    def __init__(self, films: dict[int, Film], named: Optional[dict[str, list[dict]]] = None):
        self.films = films
        self.named = named or {}  # "collection:Saga X" -> results
        self.requests: list[httpx.Request] = []
        self.fail_with: Optional[int] = None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"status_message": "nope"})
        path = request.url.path
        if path.endswith("/search/movie"):
            m = _FILM_RE.match(normalize_title(request.url.params["query"]))
            year = request.url.params.get("primary_release_year")
            film = self.films.get(int(m.group(1))) if m else None
            results = []
            if film is not None and (year is None or int(year) == film.year):
                results.append(
                    {
                        "id": film.n,
                        "title": film.title,
                        "original_title": film.title,
                        "release_date": f"{film.year}-06-01",
                        "vote_count": film.vote_count,
                    }
                )
            return httpx.Response(200, json={"page": 1, "results": results})
        for kind in ("collection", "company", "keyword"):
            if path.endswith(f"/search/{kind}"):
                return httpx.Response(
                    200, json={"page": 1, "results": self.named.get(f"{kind}:{request.url.params['query']}", [])}
                )
        if "/movie/" in path:
            film = self.films.get(int(path.rsplit("/", 1)[1]))
            if film is None:
                return httpx.Response(404, json={})
            appends = request.url.params.get("append_to_response", "").split(",")
            return httpx.Response(200, json=film.details("credits" in appends))
        return httpx.Response(404, json={})

    def client(self) -> TMDBClient:
        async def no_sleep(_):
            return None

        return TMDBClient("test-read-token", transport=self.transport, sleep=no_sleep)


_COUNT_RE = re.compile(r"Return exactly (\d+) films")
_NEEDED_DECADE_RE = re.compile(r"more film\(s\) released in the (\d{4})s")


@dataclass
class ScriptedLLM:
    """
    Suggests films in the order of ``sequence`` (film numbers), ``request_count`` per call,
    continuing where the previous call stopped; when the sequence is exhausted it wraps
    around (so a small universe makes the model "repeat itself"). ``year_of`` gives the year
    the model claims (default: the true year).
    """

    sequence: list[int]
    films: dict[int, Film]
    position: int = 0
    calls: int = 0
    prompts: list[str] = field(default_factory=list)
    fail_status: Optional[int] = None
    given: set = field(default_factory=set)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.calls += 1
        self.prompts.append(body["messages"][1]["content"])
        if self.fail_status:
            return httpx2.Response(
                self.fail_status, json={"error": {"message": f"fail {BODY_SENTINEL}", "code": self.fail_status}}
            )
        count = int(_COUNT_RE.search(body["messages"][0]["content"]).group(1))
        wanted = {int(d) for d in _NEEDED_DECADE_RE.findall(self.prompts[-1])}
        movies = []
        for n in self._pick(count, wanted):
            film = self.films.get(n)
            year = film.year if film else default_year(n)
            category = film.category if film else "alpha"
            movies.append({"title": f"Film {n}", "year": year, "category": category})
        return httpx2.Response(200, json=completion(json.dumps({"movies": movies})))

    def _pick(self, count: int, wanted_decades: set[int]) -> list[int]:
        """Next films in sequence; like a real model, favour decades the prompt says are missing."""
        out: list[int] = []
        if wanted_decades:
            for offset in range(len(self.sequence)):
                n = self.sequence[(self.position + offset) % len(self.sequence)]
                film = self.films.get(n)
                if film and film.year // 10 * 10 in wanted_decades and n not in self.given:
                    out.append(n)
                    if len(out) == count:
                        break
        while len(out) < count:
            n = self.sequence[self.position % len(self.sequence)]
            self.position += 1
            out.append(n)
        self.given.update(out)
        return out

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))


class FakeServices(Services):
    def __init__(self, llm: ScriptedLLM, tmdb: UniverseTMDB, db_path: Optional[str] = None):
        super().__init__(db_path)
        self.llm = llm
        self.tmdb = tmdb

    def make_llm(self, definition, budget):
        async def no_sleep(_):
            return None

        return CuratedLLMService(
            LLMSettings.from_definition(definition),
            budget,
            http_client=self.llm.client(),
            sleep=no_sleep,
            db_path=self.db_path,
        )

    def make_gate(self) -> TMDBGate:
        gate = TMDBGate(client=self.tmdb.client(), db_path=self.db_path)
        gate._owns_client = True
        return gate

    def make_tmdb_client(self) -> TMDBClient:
        return self.tmdb.client()


def definition(**overrides) -> dict:
    """A neutral definition: only the prompt is set unless a test sets more."""
    d = {"prompt": "Suggest {{ request_count }} films for category alpha."}
    d.update(overrides)
    return d


def ids(items: Iterable) -> list[int]:
    return [i.tmdb_id for i in items]


class SlowLLM:
    """Wraps a real ``CuratedLLMService``; each call first waits ``delay`` seconds (async)."""

    def __init__(self, inner, delay: float):
        self.inner = inner
        self.delay = delay

    async def generate(self, *args, **kwargs):
        await asyncio.sleep(self.delay)
        return await self.inner.generate(*args, **kwargs)

    async def aclose(self):
        await self.inner.aclose()


class SlowServices(FakeServices):
    def __init__(self, llm: ScriptedLLM, tmdb: UniverseTMDB, delay: float, db_path: Optional[str] = None):
        super().__init__(llm, tmdb, db_path)
        self.delay = delay

    def make_llm(self, definition, budget):
        return SlowLLM(super().make_llm(definition, budget), self.delay)
