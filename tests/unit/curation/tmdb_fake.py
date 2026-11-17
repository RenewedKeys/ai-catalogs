"""A fake TMDB served through ``httpx.MockTransport`` from the JSON fixtures."""

import copy
import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"


def load_searches() -> list[dict]:
    return json.loads((FIXTURES / "searches.json").read_text())


def load_movies() -> dict[int, dict]:
    return {int(k): v for k, v in json.loads((FIXTURES / "movies.json").read_text()).items()}


class FakeTMDB:
    """
    Answers ``/search/movie`` and ``/movie/{id}`` from fixtures. ``script`` is a list of
    canned responses (``httpx.Response`` or an exception instance) served before the
    fixtures, to simulate 429s, 5xx and timeouts. Records every request.
    """

    def __init__(self):
        self.searches = load_searches()
        self.movies = load_movies()
        self.script: list = []
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def search_calls(self) -> list[tuple[str, int | None]]:
        out = []
        for r in self.requests:
            if r.url.path.endswith("/search/movie"):
                y = r.url.params.get("primary_release_year")
                out.append((r.url.params["query"], int(y) if y else None))
        return out

    def detail_calls(self) -> list[int]:
        return [int(r.url.path.rsplit("/", 1)[1]) for r in self.requests if "/movie/" in r.url.path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        path = request.url.path
        if path.endswith("/search/movie"):
            query = request.url.params["query"]
            year = request.url.params.get("primary_release_year")
            year = int(year) if year else None
            for s in self.searches:
                if s["query"] == query and s["primary_release_year"] == year:
                    return httpx.Response(200, json={"page": 1, "results": s["results"]})
            return httpx.Response(200, json={"page": 1, "results": []})
        if "/movie/" in path:
            tmdb_id = int(path.rsplit("/", 1)[1])
            if tmdb_id not in self.movies:
                return httpx.Response(404, json={"status_message": "The resource you requested could not be found."})
            details = copy.deepcopy(self.movies[tmdb_id])
            appends = request.url.params.get("append_to_response", "").split(",")
            if "credits" not in appends:
                details.pop("credits", None)
            return httpx.Response(200, json=details)
        return httpx.Response(404)
