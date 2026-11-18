"""
Series (TV show) catalogs (PLAN.md section 29): definition, limits, gate, exclusions, LLM
request, store and a full run. Placeholder titles only ("Show N").
"""

import json
from datetime import date

import pytest

from app.core.config import settings
from app.curation import media
from app.curation.budget import Budget, BudgetLimits
from app.curation.definitions import DefinitionError, validate_definition
from app.curation.exclusions import exclusion_inputs, resolve_exclusions
from app.curation.limits import limits_from_definition
from app.curation.llm import CuratedLLMService, LLMSettings, extract_rows
from app.curation.runner import Runner
from app.curation.tmdb_gate import GateRules, Reason, TMDBGate, details_key, evaluate, resolution_key, to_candidate
from app.store import db
from app.store.catalogs import (
    SnapshotItem,
    StoreError,
    accept_definition,
    active_metas,
    config_rev_of,
    publish_snapshot,
    servable_catalogs,
    window_activation_ids,
)
from app.store.jobs import enqueue_job
from tests.unit.curation.pipeline_fakes import FakeServices, ScriptedLLM, UniverseTMDB, make_universe

PROMPT = "Suggest {{ request_count }} titles for category alpha."
TODAY = date(2026, 6, 1)


def series(**overrides) -> dict:
    return {"prompt": PROMPT, "type": "series", **overrides}


def tv_details(**overrides) -> dict:
    d = {
        "id": 501,
        "name": "Show 1",
        "original_name": "Show 1",
        "first_air_date": "2010-03-01",
        "vote_average": 8.0,
        "vote_count": 900,
        "original_language": "en",
        "genres": [{"id": 18, "name": "Drama"}],
        "adult": False,
        "status": "Ended",
        "external_ids": {"imdb_id": "tt7000001"},
        "keywords": {"results": [{"id": 77, "name": "alpha"}]},
        "production_companies": [{"id": 33}],
        "poster_path": "/s1.jpg",
        "overview": "Overview.",
    }
    d.update(overrides)
    return d


SERIES_RULES = GateRules(media_type="series")


# --------------------------------------------------------------------------- definition


def test_type_defaults_to_movie_and_absent_key_keeps_the_revision():
    canonical, model, _ = validate_definition({"prompt": PROMPT})
    assert model.type == "movie" and "type" not in canonical
    assert config_rev_of(canonical) == config_rev_of({"prompt": PROMPT})
    assert media.media_type(canonical) == "movie"


def test_series_definition_is_valid():
    canonical, model, inputs = validate_definition(
        series(year_min=2000, excluded_genre_ids=[10764], blacklist_titles=["Show 9"], decade_min={2000: 1})
    )
    assert model.type == "series" and inputs.series


@pytest.mark.parametrize(
    "extra",
    [
        {"min_runtime": 30},
        {"max_per_collection": 1},
        {"director_cap": 2},
        {"exclude_watched": True},
        {"franchise_denylist": ["Saga"]},
        {"franchise_denylist": {"collections": ["Saga"]}},
        {"excluded_genre_ids": [27]},  # Horror is a movie genre only
    ],
)
def test_movie_only_settings_are_rejected_for_series(extra):
    with pytest.raises(DefinitionError):
        validate_definition(series(**extra))


def test_movie_only_settings_still_work_for_movies():
    validate_definition({"prompt": PROMPT, "min_runtime": 80, "director_cap": 2, "excluded_genre_ids": [27]})


def test_unknown_type_is_rejected():
    with pytest.raises(DefinitionError):
        validate_definition({"prompt": PROMPT, "type": "anime"})


# --------------------------------------------------------------------------- limits / prompt wording


def test_series_limits_say_shows_and_first_aired():
    block = limits_from_definition(
        series(year_min=2001, excluded_genre_ids=[10764], decade_min={2000: 2})
    ).prompt_block()
    assert "every show must satisfy" in block
    assert "First aired in 2001 or later." in block
    assert "No shows whose genres include: Reality." in block
    assert "At least 2 show(s) first aired in the 2000s." in block
    assert "film" not in block


def test_movie_limits_are_unchanged():
    block = limits_from_definition({"year_min": 2001, "excluded_genre_ids": [27]}).prompt_block()
    assert "every film must satisfy" in block and "Released in 2001 or later." in block
    assert "No films whose genres include: Horror." in block


def test_series_gate_rules_carry_the_type():
    assert limits_from_definition(series()).gate_rules().media_type == "series"
    assert limits_from_definition({}).gate_rules().media_type == "movie"


# --------------------------------------------------------------------------- gate


def test_tv_details_pass_and_become_a_candidate():
    d = tv_details()
    assert evaluate(d, SERIES_RULES, TODAY) == []
    c = to_candidate(d, series=True)
    assert (c.title, c.year, c.imdb_id, c.runtime, c.collection_id) == ("Show 1", 2010, "tt7000001", 0, None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "Planned"},
        {"status": "In Production"},
        {"status": "Pilot"},
        {"first_air_date": "2027-01-01"},
        {"first_air_date": None},
    ],
)
def test_unaired_shows_are_not_released(overrides):
    assert Reason.NOT_RELEASED in evaluate(tv_details(**overrides), SERIES_RULES, TODAY)


@pytest.mark.parametrize("status", ["Returning Series", "Ended", "Canceled"])
def test_aired_statuses_pass(status):
    assert evaluate(tv_details(status=status), SERIES_RULES, TODAY) == []


def test_tv_rules_use_first_air_year_tv_keywords_and_names():
    from app.curation.tmdb_gate import FranchiseDenylist

    rules = GateRules(
        media_type="series",
        year_min=2011,
        franchise_denylist=FranchiseDenylist(keyword_ids={77}, title_patterns=("^show 1$",)),
    )
    reasons = evaluate(tv_details(), rules, TODAY)
    assert Reason.YEAR_OUT_OF_RANGE in reasons
    assert Reason.FRANCHISE_KEYWORD in reasons and Reason.FRANCHISE_TITLE in reasons


def test_tv_needs_an_imdb_id_and_not_adult():
    reasons = evaluate(tv_details(external_ids={}, adult=None), SERIES_RULES, TODAY)
    assert Reason.IMDB_ID_MISSING in reasons and Reason.ADULT in reasons


def test_cache_keys_never_mix_movies_and_shows():
    assert resolution_key("show 1", 2010) != resolution_key("show 1", 2010, series=True)
    assert details_key(5, False) == "details:5:base"  # existing movie rows stay valid
    assert details_key(5, False, series=True) == "details:tv:5:base"


async def test_gate_resolves_and_checks_through_the_tv_endpoints():
    films = make_universe(3)
    tmdb = UniverseTMDB(films)
    async with TMDBGate(SERIES_RULES, client=tmdb.client()) as gate:
        gate._owns_client = True
        result = await gate.check("Show 2", films[2].year)
    assert result.title == "Show 2" and result.imdb_id == films[2].imdb_id
    paths = [r.url.path for r in tmdb.requests]
    assert paths[0].endswith("/search/tv") and paths[1].endswith("/tv/2")
    assert tmdb.requests[0].url.params["first_air_date_year"] == str(films[2].year)
    assert not any("/movie" in p for p in paths)


# --------------------------------------------------------------------------- exclusions


async def test_series_blacklist_searches_tv_and_has_its_own_revision():
    movie_inputs = exclusion_inputs({"blacklist_titles": ["Show 2"]})
    tv_inputs = exclusion_inputs(series(blacklist_titles=["Show 2"]))
    assert movie_inputs.rev != tv_inputs.rev
    tmdb = UniverseTMDB(make_universe(3))
    client = tmdb.client()
    try:
        resolved = await resolve_exclusions(client, tv_inputs)
    finally:
        await client.aclose()
    assert resolved.blacklist_tmdb_ids == [2]
    assert tmdb.requests[0].url.path.endswith("/search/tv")


# --------------------------------------------------------------------------- LLM request


def _service(defn):
    budget = Budget(BudgetLimits.from_definition(defn), catalog_id="c", run_id=None)
    return CuratedLLMService(LLMSettings.from_definition(defn), budget)


def test_series_system_message_and_schema():
    svc = _service(series())
    params = svc._params("prompt", 10, "json_schema")
    system = params["messages"][0]["content"]
    assert "TV series" in system and "FIRST AIRED" in system and '{"series": [...]}' in system
    schema = params["response_format"]["json_schema"]
    assert schema["name"] == "series_list" and schema["schema"]["required"] == ["series"]
    assert '"series": [' in svc._params("prompt", 10, "json_object")["messages"][0]["content"]


def test_movie_system_message_is_unchanged():
    params = _service({"prompt": PROMPT})._params("prompt", 10, "json_schema")
    assert params["messages"][0]["content"].startswith("You are a film curator.")
    assert params["response_format"]["json_schema"]["name"] == "movie_list"


def test_series_reply_is_read():
    rows = extract_rows(json.dumps({"series": [{"title": "Show 1", "year": 2010, "category": "alpha"}]}), False)
    assert rows == [{"title": "Show 1", "year": 2010, "category": "alpha"}]


# --------------------------------------------------------------------------- store


def _items(kind, start=1, count=3):
    return [
        SnapshotItem(f"tt{8000000 + n}", f"Show {n}", 2000 + n, n, media_type=kind) for n in range(start, start + count)
    ]


def test_series_snapshot_is_served_as_series():
    with db.session() as conn, db.transaction(conn):
        rev = accept_definition(conn, "shows", "Shows", series())
        publish_snapshot(conn, "shows", _items("series"), config_rev=rev)
    with db.session() as conn:
        assert [(c.catalog_id, c.media_type) for c in servable_catalogs(conn)] == [("shows", "series")]
        assert active_metas(conn, "shows")[0]["type"] == "series"
        assert active_metas(conn, "shows", "movie") is None


def test_a_snapshot_never_mixes_types():
    with db.session() as conn, db.transaction(conn):
        rev = accept_definition(conn, "shows", "Shows", series())
        with pytest.raises(StoreError):
            publish_snapshot(conn, "shows", _items("series") + _items("movie", 10), config_rev=rev)


def test_history_window_only_counts_lists_of_the_current_type():
    with db.session() as conn, db.transaction(conn):
        rev = accept_definition(conn, "c", "C", {"prompt": PROMPT})
        publish_snapshot(conn, "c", _items("movie"), config_rev=rev)
        rev = accept_definition(conn, "c", "C", series())
        publish_snapshot(conn, "c", _items("series", 10), config_rev=rev)
    with db.session() as conn:
        all_ids = window_activation_ids(conn, "c", 10)
        assert len(all_ids) == 2
        assert window_activation_ids(conn, "c", 10, "series") == all_ids[:1]
        assert window_activation_ids(conn, "c", 10, "movie") == all_ids[1:]


def test_migration_7_marks_existing_lists_as_movies():
    with db.session() as conn:
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(snapshots)")}
    assert cols["media_type"]["dflt_value"] == "'movie'" and cols["media_type"]["notnull"] == 1


# --------------------------------------------------------------------------- full run


@pytest.fixture
def _llm_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")


async def test_a_shows_catalog_test_runs_and_publishes_series(_llm_key):
    films = make_universe(12)
    llm = ScriptedLLM(sorted(films), films)
    services = FakeServices(llm, UniverseTMDB(films))
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, "shows", "Shows", series(target_count=5, overgenerate_factor=1.0, year_min=1980))
    runner = Runner(owner="a", services=services)
    assert await runner.start()
    try:
        for kind, outcome in (("dry_run", "dry_run_ok"), ("refresh", "published")):
            with db.session() as conn:
                enqueue_job(conn, "shows", kind=kind)
            assert (await runner.run_next()).outcome == outcome
    finally:
        await runner.stop()
    assert "TV series" not in llm.prompts[0]  # the user message is the catalog's own prompt
    assert "First aired in 1980 or later." in llm.prompts[0]
    with db.session() as conn:
        metas = active_metas(conn, "shows", "series")
        preview = json.loads(conn.execute("SELECT preview_json FROM runs WHERE dry_run = 1").fetchone()[0])
    assert len(metas) == 5 and {m["type"] for m in metas} == {"series"}
    assert all(m["name"].startswith("Show ") and m["id"].startswith("tt") for m in metas)
    assert len(preview) == 5 and preview[0][0].startswith("Show ")
