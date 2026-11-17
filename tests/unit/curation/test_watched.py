"""
``exclude_watched``: the viewer's watched films from MDBList (PLAN.md section 19). A fake
MDBList (``httpx.MockTransport``) only; nothing here talks to the real service. Every film
is a neutral placeholder ("Film 1", ...).
"""

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
import pytest

from app.core.config import settings
from app.curation.definitions import DefinitionError, parse_yaml, validate_definition
from app.curation.pipeline import Pipeline, PipelineFailure, PoolItem
from app.curation.prompt import WATCHED_PREFIX, render_prompt
from app.curation.runner import Services
from app.curation.tmdb_gate import TMDBGate
from app.curation.watched import (
    WATCHED_TTL_MS,
    MDBListClient,
    WatchedError,
    WatchedSet,
    load_watched,
    parse_rows,
)
from app.store import db
from tests.unit.curation.llm_fake import API_KEY_SENTINEL
from tests.unit.curation.pipeline_fakes import IMDB_BASE, FakeServices, ScriptedLLM, UniverseTMDB, definition
from tests.unit.curation.pipeline_fakes import make_universe
from tests.unit.curation.test_pipeline import active_ids, last_run, put_definition, run_once

MDBLIST_KEY_SENTINEL = "mdblist-key-SENTINEL-0123456789"
BODY_SENTINEL = "MDBLIST-BODY-SENTINEL"
WATCHED_EPOCH = datetime(2026, 10, 28)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://llm.invalid/api/v1")
    monkeypatch.setattr(settings, "MDBLIST_API_KEY", MDBLIST_KEY_SENTINEL)


class FakeMDBList:
    """``GET /sync/watched?mediatype=movie`` over cursor pages of ``page_size`` rows."""

    def __init__(self, watched: list[int], page_size: int = 1000, imdb_only: tuple[int, ...] = ()):
        self.watched = watched  # film numbers, most recently watched first
        self.page_size = page_size
        self.imdb_only = set(imdb_only)  # rows that carry no TMDB id
        self.requests: list[httpx.Request] = []
        self.fail_with: Optional[int] = None
        self.fail_on_page: Optional[int] = None  # 1-based

    def row(self, n: int, i: int) -> dict:
        ids = {"imdb": f"tt{IMDB_BASE + n}", "trakt": 900_000 + n, "mdblist": f"m{n}"}
        if n not in self.imdb_only:
            ids["tmdb"] = n
        return {
            "last_watched_at": (WATCHED_EPOCH - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "movie": {"title": f"Film {n}", "year": 1980 + n % 45, "ids": ids},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.path == "/sync/watched"
        assert request.url.params["mediatype"] == "movie"
        assert request.url.params["apikey"] == MDBLIST_KEY_SENTINEL
        start = int(request.url.params.get("cursor", "0,0").split(",")[1])
        page = start // self.page_size + 1
        if self.fail_with and (self.fail_on_page is None or self.fail_on_page == page):
            return httpx.Response(self.fail_with, json={"error": f"nope {BODY_SENTINEL} {MDBLIST_KEY_SENTINEL}"})
        chunk = self.watched[start : start + self.page_size]
        end = start + len(chunk)
        next_cursor = f"1700000000,{end}" if end < len(self.watched) else None
        return httpx.Response(
            200,
            json={
                "movies": [self.row(n, start + i) for i, n in enumerate(chunk)],
                "shows": [],
                "pagination": {"limit": self.page_size, "next_cursor": next_cursor},
            },
        )

    def client(self) -> MDBListClient:
        async def no_sleep(_):
            return None

        return MDBListClient(transport=httpx.MockTransport(self.handle), sleep=no_sleep)


class WatchedServices(FakeServices):
    def __init__(self, llm, tmdb, mdblist: FakeMDBList):
        super().__init__(llm, tmdb)
        self.mdblist = mdblist

    def make_watched_client(self):
        if not (settings.MDBLIST_API_KEY or "").strip():
            raise WatchedError("config")
        return self.mdblist.client()


def services(films, watched, sequence=None, **mdblist_kwargs) -> WatchedServices:
    llm = ScriptedLLM(sequence or sorted(films), films)
    return WatchedServices(llm, UniverseTMDB(films), FakeMDBList(watched, **mdblist_kwargs))


def published_numbers() -> set[int]:
    return {int(i[2:]) - IMDB_BASE for i in active_ids()}


def detail_requests(tmdb: UniverseTMDB) -> set[int]:
    return {int(r.url.path.rsplit("/", 1)[1]) for r in tmdb.requests if "/movie/" in r.url.path}


# --------------------------------------------------------------------------- the filter


async def test_watched_films_are_never_published():
    films = make_universe(400)
    watched = list(range(1, 81))  # the model suggests these first
    put_definition(definition(exclude_watched=True))
    svc = services(films, watched, imdb_only=(5, 6, 7))
    report = await run_once(svc)
    assert report.outcome == "published", report
    published = published_numbers()
    assert len(published) == 100 and not published & set(watched)
    rejects = json.loads(last_run()["reject_reasons_json"])
    assert rejects["watched"] == 80
    assert json.loads(last_run()["stage_counts_json"])["watched_known"] == 80
    # Matched on the TMDB id before the details lookup; IMDb-only rows need the details.
    looked_up = detail_requests(svc.tmdb)
    assert not looked_up & (set(watched) - {5, 6, 7}) and {5, 6, 7} <= looked_up


async def test_without_the_option_watched_films_are_eligible_and_mdblist_is_not_called():
    films = make_universe(400)
    put_definition(definition())
    svc = services(films, list(range(1, 81)))
    assert (await run_once(svc)).outcome == "published"
    assert published_numbers() & set(range(1, 81))
    assert svc.mdblist.requests == []


async def test_watched_titles_are_named_in_the_prompt_and_capped():
    films = make_universe(400)
    put_definition(definition(exclude_watched=True, watched_prompt_limit=5))
    svc = services(films, list(range(300, 320)))
    assert (await run_once(svc)).outcome == "published"
    prompt = svc.llm.prompts[0]
    assert WATCHED_PREFIX in prompt and "Film 300 (" in prompt and "Film 304 (" in prompt
    assert "Film 305 (" not in prompt  # only the 5 most recently watched


async def test_relax_mode_never_readmits_watched_films():
    films = make_universe(150)
    put_definition(definition(history_mode="relax", exclude_watched=True))
    assert (await run_once(services(films, [149, 150]))).outcome == "published"
    report = await run_once(services(films, [149, 150]))
    assert report.outcome == "published" and last_run()["relaxed_window"] == 0
    assert not published_numbers() & {149, 150}


def test_assert_publishable_rejects_a_watched_film():
    films = make_universe(3)
    pipeline = Pipeline(
        catalog_id="example",
        definition=definition(exclude_watched=True, target_count=2),
        exclusions_json=None,
        seed="0" * 16,
        today=date(2026, 1, 1),
        llm=None,
        gate=TMDBGate(),
    )
    pipeline._details = {n: f.details(False) for n, f in films.items()}
    items = [PoolItem(n, f"tt{IMDB_BASE + n}", films[n].year, None, None) for n in (1, 2)]
    pipeline.watched = WatchedSet(frozenset(), frozenset())
    assert len(pipeline.assert_publishable(items, 2, {})) == 2
    pipeline.watched = WatchedSet(frozenset(), frozenset({f"tt{IMDB_BASE + 2}"}))
    with pytest.raises(PipelineFailure) as e:
        pipeline.assert_publishable(items, 2, {})
    assert e.value.category == "assertion"
    pipeline.watched = None  # the option is on but the set never loaded: never publish
    with pytest.raises(PipelineFailure):
        pipeline.assert_publishable(items, 2, {})


# --------------------------------------------------------------------------- cache


async def test_cache_is_reused_within_its_ttl_and_refreshed_after(monkeypatch):
    offset = {"ms": 0}
    monkeypatch.setattr(db, "clock", lambda: time.time() + offset["ms"] / 1000)
    films = make_universe(1000)
    put_definition(definition(exclude_watched=True))
    first = services(films, list(range(901, 3401)))  # three pages; films 901-1000 exist
    assert (await run_once(first)).outcome == "published"
    assert len(first.mdblist.requests) == 3
    second = services(films, [999])  # MDBList changed, but the cache is still fresh
    assert (await run_once(second)).outcome == "published"
    assert second.mdblist.requests == []
    assert json.loads(last_run()["stage_counts_json"])["watched_known"] == 2500
    offset["ms"] = WATCHED_TTL_MS + 1
    third = services(films, [999])
    assert (await run_once(third)).outcome == "published"
    assert len(third.mdblist.requests) == 1
    assert json.loads(last_run()["stage_counts_json"])["watched_known"] == 1


async def test_a_failed_page_caches_nothing():
    mdb = FakeMDBList(list(range(1, 2501)))
    mdb.fail_with, mdb.fail_on_page = 503, 2
    with pytest.raises(WatchedError) as e:
        await load_watched(mdb.client)
    assert e.value.category == "mdblist_http_5xx" and e.value.transient
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM watched_cache").fetchone()[0] == 0
    mdb.fail_with = None
    assert len(await load_watched(mdb.client)) == 2500


# --------------------------------------------------------------------------- failures


async def test_missing_key_is_a_clear_config_error_before_any_model_call(monkeypatch):
    monkeypatch.setattr(settings, "MDBLIST_API_KEY", None)
    films = make_universe(200)
    put_definition(definition(exclude_watched=True))
    svc = services(films, [1])
    report = await run_once(svc)
    assert report.outcome == "failed" and report.category == "config" and report.job_state == "failed"
    assert "MDBLIST_API_KEY" in report.detail
    assert svc.llm.calls == 0


def test_the_real_services_need_the_key(monkeypatch):
    monkeypatch.setattr(settings, "MDBLIST_API_KEY", "  ")
    with pytest.raises(WatchedError) as e:
        Services().make_watched_client()
    assert e.value.category == "config"


async def test_mdblist_5xx_is_transient_and_keeps_the_previous_snapshot(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    offset = {"ms": 0}
    monkeypatch.setattr(db, "clock", lambda: time.time() + offset["ms"] / 1000)
    films = make_universe(400)
    put_definition(definition(exclude_watched=True))
    assert (await run_once(services(films, [1, 2]))).outcome == "published"
    before = active_ids()
    offset["ms"] = WATCHED_TTL_MS + 1  # cache expired: the run must refetch
    svc = services(films, [1, 2])
    svc.mdblist.fail_with = 503
    report = await run_once(svc)
    assert report.outcome == "failed" and report.category == "mdblist_http_5xx"
    assert report.job_state == "pending"  # retried later
    assert active_ids() == before
    assert svc.llm.calls == 0
    run = last_run()
    assert run["status"] == "failed" and run["error_status"] == 503
    for text in (caplog.text, json.dumps(run), report.detail):
        assert MDBLIST_KEY_SENTINEL not in text and BODY_SENTINEL not in text


@pytest.mark.parametrize("status, category, transient", [(401, "mdblist_auth", False), (429, "mdblist_rate_limit", True)])
async def test_auth_and_rate_limit_errors(status, category, transient, caplog):
    caplog.set_level(logging.DEBUG)
    put_definition(definition(exclude_watched=True))
    svc = services(make_universe(200), [1])
    svc.mdblist.fail_with = status
    report = await run_once(svc)
    assert report.category == category
    assert report.job_state == ("pending" if transient else "failed")
    assert MDBLIST_KEY_SENTINEL not in caplog.text and BODY_SENTINEL not in caplog.text


async def test_a_long_retry_after_is_not_waited_out():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "36000"}, json={"error": "Daily API limit exceeded!"})

    async def no_sleep(_):
        raise AssertionError("must not wait for the daily reset")

    client = MDBListClient(transport=httpx.MockTransport(handle), sleep=no_sleep)
    with pytest.raises(WatchedError) as e:
        await client.watched_movies()
    assert e.value.category == "mdblist_rate_limit" and len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"shows": []},  # no movies list: never read as "nothing watched"
        {"movies": "x"},
        {"movies": [{"movie": {"ids": {"tmdb": 1}}}], "pagination": {"next_cursor": "same"}},
    ],
)
async def test_unexpected_payloads_are_schema_errors(body):
    def handle(request):
        return httpx.Response(200, json=body)

    client = MDBListClient(transport=httpx.MockTransport(handle))
    with pytest.raises(WatchedError) as e:
        await client.watched_movies()
    assert e.value.category == "mdblist_schema" and not e.value.transient


def test_rows_parse_both_shapes_and_skip_rows_without_ids():
    watched = parse_rows(
        [
            {"last_watched_at": "2026-01-02T00:00:00Z", "movie": {"title": "Film 1", "ids": {"tmdb": 1}}},
            {"watched_at": "2026-01-03T00:00:00Z", "movie": {"title": "Film 2", "ids": {"imdb": "tt2"}}},
            {"id": 3, "tmdb": 3, "last_watched_at": "2026-01-01T00:00:00Z"},  # extended=ids_only
            {"movie": {"title": "Film 4", "ids": {"trakt": 4}}},  # no usable id
            {"movie": {"title": "Film 5", "ids": {"tmdb": True}}},
            "garbage",
        ]
    )
    assert watched.tmdb_ids == {1, 3} and watched.imdb_ids == {"tt2"}
    assert watched.titles == (("Film 2", None), ("Film 1", None))  # most recently watched first


def test_watched_block_is_cut_before_the_history_block():
    defn = definition(max_prompt_chars=1000)
    history = [(f"Film {i}", 2000) for i in range(1, 30)]
    watched = [(f"Film {i}", 1990) for i in range(100, 150)]
    rendered = render_prompt(
        defn, today=date(2026, 1, 1), seed="0" * 16, history=history, watched=watched
    )
    assert len(rendered.text) <= 1000
    assert rendered.watched_lines_used < rendered.watched_lines_total
    assert rendered.watched_lines_used == 0 or rendered.exclusion_lines_used == rendered.exclusion_lines_total


def test_watched_prompt_limit_zero_turns_the_block_off():
    rendered = render_prompt(
        definition(watched_prompt_limit=0),
        today=date(2026, 1, 1),
        seed="0" * 16,
        watched=[("Film 1", 2000)],
    )
    assert WATCHED_PREFIX not in rendered.text


# --------------------------------------------------------------------------- definition


PROMPT = "prompt: 'Suggest {{ request_count }} films for category alpha.'\n"


def test_definition_accepts_exclude_watched():
    canonical, model, _ = validate_definition(parse_yaml(PROMPT + "exclude_watched: true\nwatched_prompt_limit: 20\n"))
    assert model.exclude_watched is True and canonical["exclude_watched"] is True
    _, model, _ = validate_definition(parse_yaml(PROMPT))
    assert model.exclude_watched is False  # off by default


@pytest.mark.parametrize(
    "extra",
    ["exclude_watched: 'yes'\n", "exclude_watched: 1\n", "watched_prompt_limit: -1\n", "watched_prompt_limit: 501\n"],
)
def test_definition_rejects_bad_watched_fields(extra):
    with pytest.raises(DefinitionError):
        validate_definition(parse_yaml(PROMPT + extra))


def test_the_template_may_place_the_watched_block():
    validate_definition(parse_yaml("prompt: 'Suggest films. {{ watched }}'\nexclude_watched: true\n"))
