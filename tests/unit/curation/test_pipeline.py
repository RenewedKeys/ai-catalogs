"""
The refresh pipeline end to end through the leased runner, with a fake LLM and a fake TMDB
(PLAN.md sections 5 and 13). Every catalog here is a neutral placeholder.
"""

import json

import pytest

from app.core.config import settings
from app.curation.limits import Limits
from app.curation.pipeline import PoolItem, batch_sizes, scaled_quotas, select
from app.curation.runner import Runner
from app.store import db
from app.store.catalogs import accept_definition, active_metas
from app.store.jobs import enqueue_job
from tests.unit.curation.llm_fake import API_KEY_SENTINEL, BODY_SENTINEL
from tests.unit.curation.pipeline_fakes import FakeServices, ScriptedLLM, UniverseTMDB, definition, make_universe

CID = "example"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://llm.invalid/api/v1")


def put_definition(defn, cid=CID):
    with db.session() as conn, db.transaction(conn):
        return accept_definition(conn, cid, "Example", defn)


async def run_once(services, cid=CID, kind="refresh", **runner_kwargs):
    with db.session() as conn:
        enqueue_job(conn, cid, kind=kind)
    runner = Runner(owner="test", services=services, heartbeat_s=0.05, **runner_kwargs)
    assert await runner.start()
    try:
        return await runner.run_next()
    finally:
        await runner.stop()


def services_for(films, sequence=None):
    llm = ScriptedLLM(sequence or sorted(films), films)
    return FakeServices(llm, UniverseTMDB(films))


def active_ids(cid=CID):
    with db.session() as conn:
        metas = active_metas(conn, cid)
    return [m["id"] for m in metas] if metas is not None else None


def last_run(cid=CID):
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM runs WHERE catalog_id = ? ORDER BY id DESC LIMIT 1", (cid,)).fetchone())


def table_state(conn):
    out = {}
    for table in ("snapshots", "activations", "history", "schedule", "catalog_defs"):
        out[table] = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
    return out


# --------------------------------------------------------------------------- happy path


async def test_happy_path_publishes_exactly_100_unique():
    films = make_universe(400)
    put_definition(definition())
    services = services_for(films)
    report = await run_once(services)
    assert report.outcome == "published", report
    published = active_ids()
    assert len(published) == 100 and len(set(published)) == 100
    run = last_run()
    assert run["status"] == "ok" and run["seed"] and run["prompt_hash"]
    assert run["model_calls"] == 4  # 200 requested in batches of 50
    assert services.llm.calls == 4
    counts = json.loads(run["stage_counts_json"])
    assert counts["generated"] == 200 and counts["eligible"] == 200 and counts["selected"] == 100
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 100
        job = conn.execute("SELECT state FROM jobs").fetchone()
        assert job["state"] == "done"


def test_batches_are_at_most_60():
    assert batch_sizes(160) == [54, 53, 53]
    assert batch_sizes(60) == [60]
    assert batch_sizes(1) == [1]
    assert batch_sizes(64) == [64]  # a 40-movie catalog: one call, not 60 + 4
    assert batch_sizes(79) == [79]
    assert batch_sizes(80) == [40, 40]
    assert batch_sizes(130) == [65, 65]
    assert batch_sizes(400) == [58, 57, 57, 57, 57, 57, 57]
    assert batch_sizes(0) == []
    for total in range(1, 401):
        sizes = batch_sizes(total)
        assert sum(sizes) == total and max(sizes) < 80 and max(sizes) - min(sizes) <= 1


async def test_two_consecutive_runs_share_zero_ids():
    films = make_universe(1000)
    put_definition(definition())
    assert (await run_once(services_for(films))).outcome == "published"
    first = set(active_ids())
    # The model starts from the same titles again: the history filter must hold them.
    assert (await run_once(services_for(films))).outcome == "published"
    second = set(active_ids())
    assert len(second) == 100 and not first & second
    counts = json.loads(last_run()["stage_counts_json"])
    assert counts["held_for_history"] > 0


async def test_history_titles_are_named_in_the_prompt():
    films = make_universe(400)
    put_definition(definition())
    await run_once(services_for(films))
    services = services_for(films)
    await run_once(services)
    assert "DO NOT INCLUDE:" in services.llm.prompts[0]


# --------------------------------------------------------------------------- publish decision


async def test_strict_shortfall_fails_and_keeps_previous_snapshot_and_history():
    films = make_universe(400)
    put_definition(definition())
    assert (await run_once(services_for(films))).outcome == "published"
    before_ids = active_ids()
    with db.session() as conn:
        before = table_state(conn)
    # The model only knows the 100 titles already published: every suggestion is history.
    report = await run_once(services_for(films, sequence=[int(i[2:]) - 1_000_000 for i in before_ids]))
    assert report.outcome == "failed" and report.category == "shortfall"
    assert active_ids() == before_ids
    with db.session() as conn:
        after = table_state(conn)
        job = conn.execute("SELECT state, state_detail FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert after["snapshots"] == before["snapshots"] and after["history"] == before["history"]
    assert after["activations"] == before["activations"]
    assert job["state"] == "failed" and job["state_detail"] == "shortfall"  # not transient: no retry
    run = last_run()
    assert run["status"] == "failed" and run["error_category"] == "shortfall"
    counts = json.loads(run["stage_counts_json"])
    assert counts["selected"] == 0 and counts["held_for_history"] == 100
    assert counts["topup_rounds"] >= 1  # it tried, then stopped on stalling


async def test_relax_mode_records_window_and_repeats():
    films = make_universe(150)
    put_definition(definition(history_mode="relax"))
    assert (await run_once(services_for(films))).outcome == "published"
    first = set(active_ids())
    report = await run_once(services_for(films))
    assert report.outcome == "published"
    second = set(active_ids())
    assert len(second) == 100
    assert len(first & second) == 50  # only 50 unseen films exist
    run = last_run()
    assert run["relaxed_window"] == 0 and run["repeats"] == 50


async def test_relax_shrinks_oldest_activation_first():
    films = make_universe(300)
    defn = definition(history_mode="relax", target_count=100, history_window_runs=7)
    put_definition(defn)
    # Run 1 shows films 1..100, run 2 films 101..200 (the model suggests only those).
    assert (await run_once(services_for(films, sequence=list(range(1, 101))))).outcome == "published"
    assert (await run_once(services_for(films, sequence=list(range(101, 201))))).outcome == "published"
    # Run 3: the model knows 1..200 only. Dropping the oldest activation frees 1..100.
    report = await run_once(services_for(films, sequence=list(range(1, 201))))
    assert report.outcome == "published"
    third = {int(i[2:]) - 1_000_000 for i in active_ids()}
    assert third == set(range(1, 101))
    run = last_run()
    assert run["relaxed_window"] == 1 and run["repeats"] == 100


async def test_publish_partial_accepts_min_accept():
    films = make_universe(70)
    put_definition(definition(on_shortfall="publish_partial", min_accept=60))
    report = await run_once(services_for(films))
    assert report.outcome == "published" and report.partial and report.items == 70
    assert len(active_ids()) == 70


async def test_publish_partial_below_min_accept_fails():
    films = make_universe(40)
    put_definition(definition(on_shortfall="publish_partial", min_accept=60))
    report = await run_once(services_for(films))
    assert report.outcome == "failed" and report.category == "shortfall"
    assert active_ids() is None


# --------------------------------------------------------------------------- caps, quotas, top-up


async def test_topup_shortfall_is_computed_after_selection():
    # 160 eligible films, but 120 of them share one collection: with max_per_collection 1 the
    # first selection has only 41 items, so top-ups must follow although 160 > 100 passed.
    films = make_universe(500, collection_id=lambda n: 777 if n <= 120 else None)
    put_definition(definition(max_per_collection=1))
    services = services_for(films)
    report = await run_once(services)
    assert report.outcome == "published"
    assert services.llm.calls > 3
    published = [int(i[2:]) - 1_000_000 for i in active_ids()]
    assert sum(1 for n in published if n <= 120) == 1
    assert "STILL NEEDED" in services.llm.prompts[4]


async def test_hard_decade_quota_steers_topups_and_is_met():
    films = make_universe(600)
    put_definition(definition(decade_min={"1980s": 40}))
    services = services_for(films)
    report = await run_once(services)
    assert report.outcome == "published", (report, last_run())
    years = [films[int(i[2:]) - 1_000_000].year for i in active_ids()]
    assert sum(1 for y in years if 1980 <= y <= 1989) >= 40
    assert any("released in the 1980s" in p for p in services.llm.prompts[3:])


async def test_unmet_hard_quota_fails_in_strict_mode():
    films = make_universe(600, year=lambda n: 2000 + n % 20)  # no 1980s films at all
    put_definition(definition(decade_min={"1980": 5}, max_model_calls_per_run=5))
    report = await run_once(services_for(films))
    assert report.outcome == "failed" and report.category == "shortfall"
    assert "1980s" in report.detail


async def test_director_cap_holds():
    films = make_universe(500, director_ids=lambda n: (n % 30,))
    put_definition(definition(director_cap=2, target_count=50))
    report = await run_once(services_for(films))
    assert report.outcome == "published"
    directors = [films[int(i[2:]) - 1_000_000].director_ids[0] for i in active_ids()]
    assert max(directors.count(d) for d in set(directors)) <= 2


async def test_gate_thresholds_come_from_the_definition():
    films = make_universe(400, vote_average=lambda n: 5.0 if n % 2 else 8.0)
    put_definition(definition(min_vote_average=6.0))
    report = await run_once(services_for(films))
    assert report.outcome == "published"
    assert all((int(i[2:]) - 1_000_000) % 2 == 0 for i in active_ids())
    rejects = json.loads(last_run()["reject_reasons_json"])
    assert rejects["rating_low"] > 0


async def test_blacklist_titles_are_excluded_without_any_apply_step():
    films = make_universe(300)
    put_definition(
        definition(blacklist_titles=["Film 1", "Film 2 (1982)"], franchise_denylist={"title_patterns": ["^Film 3$"]})
    )
    report = await run_once(services_for(films))
    assert report.outcome == "published"
    published = {int(i[2:]) - 1_000_000 for i in active_ids()}
    assert not published & {1, 2, 3}
    rejects = json.loads(last_run()["reject_reasons_json"])
    assert rejects["blacklisted_title"] == 2 and rejects["franchise_title"] == 1


# --------------------------------------------------------------------------- errors and budget


async def test_dry_run_mutates_only_runs_ledger_and_its_job():
    films = make_universe(400)
    put_definition(definition())
    assert (await run_once(services_for(films))).outcome == "published"
    with db.session() as conn:
        before = table_state(conn)
        ledger_before = conn.execute("SELECT COUNT(*) FROM llm_ledger").fetchone()[0]
    report = await run_once(services_for(films), kind="dry_run")
    assert report.outcome == "dry_run_ok" and report.items == 100
    with db.session() as conn:
        assert table_state(conn) == before
        ledger_after = conn.execute("SELECT COUNT(*) FROM llm_ledger").fetchone()[0]
    run = last_run()
    assert run["dry_run"] == 1 and run["status"] == "ok" and run["model_calls"] >= 3
    assert ledger_after == ledger_before + run["model_calls"]


async def test_budget_exhausted_before_any_call_fails_as_budget():
    put_definition(definition(max_model_calls_per_run=0))
    report = await run_once(services_for(make_universe(10)))
    assert report.outcome == "failed" and report.category == "budget"


async def test_daily_budget_stops_topups_then_publish_decision_applies(monkeypatch):
    monkeypatch.setattr(settings, "MAX_MODEL_CALLS_PER_DAY", 3)
    films = make_universe(500, collection_id=lambda n: 777 if n <= 120 else None)
    put_definition(definition(max_per_collection=1))
    report = await run_once(services_for(films))
    assert report.outcome == "failed" and report.category == "shortfall"
    assert json.loads(last_run()["stage_counts_json"])["stopped_by_budget"] == 1


async def test_provider_auth_error_fails_without_retry():
    films = make_universe(100)
    put_definition(definition())
    services = services_for(films)
    services.llm.fail_status = 401
    report = await run_once(services)
    assert report.outcome == "failed" and report.category == "provider_auth" and report.job_state == "failed"
    run = last_run()
    assert run["error_status"] == 401
    assert BODY_SENTINEL not in json.dumps(run)  # provider text is never stored


async def test_transient_tmdb_error_retries_later():
    films = make_universe(100)
    put_definition(definition())
    services = services_for(films)
    services.tmdb.fail_with = 503
    report = await run_once(services)
    assert report.category == "provider_http_5xx" and report.job_state == "pending"


async def test_definition_changed_mid_run_is_superseded_with_one_replacement():
    films = make_universe(400)
    put_definition(definition())
    services = services_for(films)
    original = services.llm.handle

    def change_then_answer(request):
        if services.llm.calls == 1:
            put_definition(definition(target_count=90))
        return original(request)

    services.llm.handle = change_then_answer
    report = await run_once(services)
    assert report.outcome == "superseded"
    assert active_ids() is None
    with db.session() as conn:
        jobs = conn.execute("SELECT state, state_detail FROM jobs ORDER BY id").fetchall()
    assert [tuple(j) for j in jobs] == [("done", "superseded"), ("pending", None)]


# --------------------------------------------------------------------------- selection (pure)


def pool(n, **fields):
    return [
        PoolItem(
            tmdb_id=i,
            imdb_id=f"tt{i}",
            year=fields.get("year", lambda i: 2000)(i),
            collection_id=fields.get("coll", lambda i: None)(i),
            director_ids=fields.get("dirs", lambda i: (i,))(i),
            category=fields.get("cat", lambda i: "alpha")(i),
        )
        for i in range(1, n + 1)
    ]


def test_selection_concentrated_collections_and_independent_missing_ids():
    items = pool(50, coll=lambda i: (i % 3) + 1 if i <= 30 else None)
    s = select(items, 100, Limits(max_per_collection=1), "seed")
    assert len(s.selected) == 3 + 20


def test_selection_is_deterministic_regardless_of_pool_order():
    items = pool(80)
    a = select(items, 30, Limits(), "abc")
    b = select(list(reversed(items)), 30, Limits(), "abc")
    c = select(items, 30, Limits(), "other")
    assert [p.tmdb_id for p in a.selected] == [p.tmdb_id for p in b.selected]
    assert [p.tmdb_id for p in a.selected] != [p.tmdb_id for p in c.selected]


def test_selection_fills_hard_quota_first_and_reports_unmet():
    items = pool(100, year=lambda i: 1985 if i <= 5 else 2005)
    s = select(items, 20, Limits(decade_min={1980: 8}), "seed")
    assert sum(1 for p in s.selected if p.decade == 1980) == 5
    assert s.unmet_hard == {1980: 3} and len(s.selected) == 20


def test_selection_soft_category_quota_ranks_but_never_blocks():
    items = pool(100, cat=lambda i: "beta" if i <= 4 else "alpha")
    s = select(items, 10, Limits(category_min={"Beta": 6}), "seed")
    assert sum(1 for p in s.selected if p.category == "beta") == 4
    assert s.unmet_soft == {"Beta": 2} and not s.unmet_hard and len(s.selected) == 10


def test_partial_quotas_scale_down():
    assert scaled_quotas(Limits(decade_min={1980: 20, 1990: 5}), 60, 100) == {1980: 12, 1990: 3}


# --------------------------------------------------------------------------- dedupe and ordering


async def test_adjacent_year_guesses_collapse_on_the_resolved_id():
    import httpx2

    from tests.unit.curation.llm_fake import completion

    films = make_universe(30)
    put_definition(definition(target_count=20, overgenerate_factor=1.0))
    services = services_for(films)
    rows = [{"title": f"Film {n}", "year": films[n].year, "category": "alpha"} for n in range(1, 21)]
    rows += [{"title": "Film 5", "year": films[5].year + 1, "category": "alpha"}]  # same film, wrong year

    def reply(request):
        services.llm.calls += 1
        return httpx2.Response(200, json=completion(json.dumps({"movies": rows})))

    services.llm.handle = reply
    report = await run_once(services)
    assert report.outcome == "published"
    published = active_ids()
    assert len(published) == 20 and len(set(published)) == 20
    assert json.loads(last_run()["stage_counts_json"])["duplicate_id"] == 1


async def test_relax_counts_a_title_by_its_most_recent_activation():
    from app.store.catalogs import SnapshotItem, publish_snapshot

    films = make_universe(200)
    rev = put_definition(definition(history_mode="relax", target_count=50))

    def show(numbers):
        items = [SnapshotItem(films[n].imdb_id, films[n].title, films[n].year, n) for n in numbers]
        with db.session() as conn, db.transaction(conn):
            publish_snapshot(conn, CID, items, config_rev=rev)

    show(range(1, 101))  # older activation: films 1..100
    show(range(51, 151))  # newer activation: films 51..150 (51..100 shown again)
    report = await run_once(services_for(films, sequence=list(range(1, 151))))
    assert report.outcome == "published"
    # Dropping the older activation frees 1..50 only: 51..100 are still in the newer one.
    assert {int(i[2:]) - 1_000_000 for i in active_ids()} == set(range(1, 51))
    assert last_run()["relaxed_window"] == 1
