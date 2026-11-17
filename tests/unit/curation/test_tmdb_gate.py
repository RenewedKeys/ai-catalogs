"""TMDB resolver and gatekeeper (PLAN.md section 8), against fixture responses."""

import asyncio
import copy
import logging
from datetime import date

import httpx
import pytest

from app.curation import tmdb_gate as g
from app.curation.tmdb_gate import (
    Candidate,
    FranchiseDenylist,
    GateRules,
    Reason,
    Rejection,
    TMDBClient,
    TMDBError,
    TMDBGate,
    evaluate,
    normalize_title,
    split_title_year,
)
from app.store import db
from tests.unit.curation.tmdb_fake import FakeTMDB, load_movies

TOKEN = "test-token-SENTINEL-7f3a"
T0 = 1_800_000_000.0
DAY = 24 * 60 * 60


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(db, "clock", c)
    return c


@pytest.fixture
def fake():
    return FakeTMDB()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
async def make_gate(fake, sleeps, clock):
    clients = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    def factory(rules=None, concurrency=8):
        client = TMDBClient(TOKEN, transport=fake.transport, sleep=record_sleep, concurrency=concurrency)
        clients.append(client)
        return TMDBGate(rules or GateRules(), client=client)

    yield factory
    for c in clients:
        await c.aclose()


# --------------------------------------------------------------------------- matching


class TestNormalisation:
    def test_punctuation_case_accents_and_ampersand(self):
        assert normalize_title("Mission: Impossible") == normalize_title("mission impossible")
        assert normalize_title("Amélie") == "amelie"
        assert normalize_title("Bill & Ted's") == normalize_title("Bill and Ted s")

    def test_trailing_year_is_split(self):
        assert split_title_year("Modern (2014)", None) == ("Modern", 2014)
        assert split_title_year("Modern (2014)", 2013) == ("Modern", 2013)
        assert split_title_year("(2014)", None) == ("(2014)", None)
        assert split_title_year("1984", 1984) == ("1984", 1984)

    def test_control_characters_and_length(self):
        assert g.clean_title("The\x00 Thing\n") == "The Thing"
        assert len(g.clean_title("x" * 1000)) == g.MAX_TITLE_LENGTH


class TestResolve:
    async def test_modern_2014_never_takes_results_0(self, make_gate, fake):
        gate = make_gate()
        # Every search returns hits (superstrings within one year, Modern Times first in the
        # yearless search); none is acceptable.
        assert await gate.resolve("Modern (2014)", None) is None
        assert fake.search_calls() == [("Modern", 2014), ("Modern", 2013), ("Modern", 2015), ("Modern", None)]
        result = await gate.check("Modern", 2014)
        assert isinstance(result, Rejection) and result.reason is Reason.NO_MATCH

    @pytest.mark.parametrize(
        "title,year,expected",
        [
            ("Total Recall", 1990, 861),
            ("Total Recall", 2012, 64635),
            ("The Thing", 1982, 1091),
            ("The Thing", 2011, 60935),
        ],
    )
    async def test_same_title_remakes_resolve_by_year(self, make_gate, fake, title, year, expected):
        assert await make_gate().resolve(title, year) == expected
        assert fake.search_calls() == [(title, year)]

    async def test_adjacent_year_guess_falls_back(self, make_gate, fake):
        # LLM says 1991; no 1991 hit; 1990 (year - 1) is tried next and is within +/- 1.
        assert await make_gate().resolve("Total Recall", 1991) == 861
        assert fake.search_calls() == [("Total Recall", 1991), ("Total Recall", 1990)]

    async def test_yearless_search_still_applies_year_window(self, make_gate, fake):
        # 2010 and 2009 searches find nothing; 2011 finds the prequel. Remove the 2011 entry
        # so only the yearless search answers: it must still pick the 2011 film, not the
        # 1982 one with more votes or the dateless one with the most.
        fake.searches = [s for s in fake.searches if s["primary_release_year"] != 2011]
        assert await make_gate().resolve("The Thing", 2010) == 60935
        assert fake.search_calls()[-1] == ("The Thing", None)

    async def test_dateless_candidate_is_never_acceptable(self, make_gate, fake):
        fake.searches = [s for s in fake.searches if s["primary_release_year"] is None]
        for s in fake.searches:
            if s["query"] == "The Thing":
                s["results"] = [r for r in s["results"] if r["release_date"] == ""]
        assert await make_gate().resolve("The Thing", 1982) is None

    async def test_highest_vote_count_wins_among_acceptable(self, make_gate, fake):
        fake.searches.append(
            {
                "query": "Twins",
                "primary_release_year": 1988,
                "results": [
                    {
                        "id": 1,
                        "title": "Twins",
                        "original_title": "Twins",
                        "release_date": "1988-12-09",
                        "vote_count": 10,
                    },
                    {
                        "id": 9493,
                        "title": "Twins",
                        "original_title": "Twins",
                        "release_date": "1988-12-09",
                        "vote_count": 3000,
                    },
                    {
                        "id": 2,
                        "title": "Twinsanity",
                        "original_title": "x",
                        "release_date": "1988-01-01",
                        "vote_count": 99999,
                    },
                ],
            }
        )
        assert await make_gate().resolve("Twins", 1988) == 9493

    async def test_alias_pair_title_and_original_title(self, make_gate):
        gate = make_gate()
        assert await gate.resolve("Brotherhood of the Wolf", 2001) == 6312
        assert await gate.resolve("Le Pacte des loups", 2001) == 6312

    async def test_near_spelling_is_accepted_by_similarity(self, make_gate, fake):
        fake.searches.append(
            {
                "query": "Brotherhood of the Wolves",
                "primary_release_year": 2001,
                "results": next(s for s in fake.searches if s["query"] == "Brotherhood of the Wolf")["results"],
            }
        )
        assert await make_gate().resolve("Brotherhood of the Wolves", 2001) == 6312

    async def test_empty_title_is_no_match_without_a_request(self, make_gate, fake):
        assert await make_gate().resolve("  ?! ", 1990) is None
        assert fake.requests == []


# --------------------------------------------------------------------------- check


class TestCheck:
    async def test_film_with_no_collection_is_accepted(self, make_gate):
        result = await make_gate().check("Total Recall", 1990)
        assert isinstance(result, Candidate)
        assert result.tmdb_id == 861 and result.imdb_id == "tt0100802" and result.year == 1990
        assert result.collection_id is None
        assert result.director_ids is None  # credits not requested
        assert result.genre_ids == (28, 12, 878)

    async def test_collection_id_is_reported(self, make_gate):
        result = await make_gate().check("Mad Max 2", 1981)
        assert isinstance(result, Candidate) and result.collection_id == 8945

    async def test_credits_give_director_ids(self, make_gate, fake):
        result = await make_gate(GateRules(include_credits=True)).check("Total Recall", 1990)
        assert result.director_ids == (10491,)
        assert fake.requests[-1].url.params["append_to_response"] == "external_ids,keywords,credits"

    async def test_details_request_appends_ids_and_keywords(self, make_gate, fake):
        await make_gate().check("The Thing", 1982)
        assert fake.requests[-1].url.params["append_to_response"] == "external_ids,keywords"

    async def test_rejection_carries_reason_and_id(self, make_gate):
        result = await make_gate().check("Brotherhood of the Wolf", 2001)
        assert isinstance(result, Rejection)
        assert result.reason is Reason.LANGUAGE and result.tmdb_id == 6312

    async def test_details_404_is_not_found(self, make_gate, fake):
        fake.searches.append(
            {
                "query": "Ghost",
                "primary_release_year": 1990,
                "results": [
                    {
                        "id": 424242,
                        "title": "Ghost",
                        "original_title": "Ghost",
                        "release_date": "1990-07-13",
                        "vote_count": 1,
                    }
                ],
            }
        )
        result = await make_gate().check("Ghost", 1990)
        assert isinstance(result, Rejection) and result.reason is Reason.NOT_FOUND


# --------------------------------------------------------------------------- gatekeeper


def base_details():
    return copy.deepcopy(load_movies()[861])


TODAY = date(2026, 11, 16)


def reasons_for(mutate, rules=None):
    d = base_details()
    mutate(d)
    return evaluate(d, rules or GateRules(), today=TODAY)


class TestGatekeeper:
    def test_base_film_passes(self):
        assert reasons_for(lambda d: None) == []

    @pytest.mark.parametrize(
        "mutate,reason",
        [
            (lambda d: d.update(runtime=None), Reason.RUNTIME_MISSING),
            (lambda d: d.pop("runtime"), Reason.RUNTIME_MISSING),
            (lambda d: d.update(runtime=0), Reason.RUNTIME_MISSING),
            (lambda d: d.update(runtime=69), Reason.RUNTIME_SHORT),
            (lambda d: d.update(vote_average=6.19), Reason.RATING_LOW),
            (lambda d: d.update(vote_count=999), Reason.VOTES_LOW),
            (lambda d: d.update(original_language="fr"), Reason.LANGUAGE),
            (lambda d: d["genres"].append({"id": 16}), Reason.GENRE_EXCLUDED),
            (lambda d: d["genres"].append({"id": 10770}), Reason.GENRE_EXCLUDED),
            (lambda d: d["genres"].append({"id": 10751}), Reason.GENRE_EXCLUDED),
            (lambda d: d["genres"].append({"id": 99}), Reason.GENRE_EXCLUDED),
            (lambda d: d.update(release_date=""), Reason.YEAR_MISSING),
            (lambda d: d.update(release_date="1979-12-31"), Reason.YEAR_OUT_OF_RANGE),
            (lambda d: d.update(release_date="2027-01-01"), Reason.YEAR_OUT_OF_RANGE),
            (lambda d: d.update(adult=True), Reason.ADULT),
            (lambda d: d.pop("adult"), Reason.ADULT),
            (lambda d: d.update(status="Post Production"), Reason.NOT_RELEASED),
            (lambda d: d.update(imdb_id=None, external_ids={"imdb_id": ""}), Reason.IMDB_ID_MISSING),
            (lambda d: d.update(imdb_id=None, external_ids={"imdb_id": None}), Reason.IMDB_ID_MISSING),
            (lambda d: d.update(imdb_id="nm0000216", external_ids={"imdb_id": "nm0000216"}), Reason.IMDB_ID_MISSING),
            (lambda d: d.update(imdb_id=None, external_ids={"imdb_id": "tt01x"}), Reason.IMDB_ID_MISSING),
        ],
    )
    def test_each_rule(self, mutate, reason):
        assert reasons_for(mutate) == [reason]

    def test_boundaries_pass(self):
        def edge(d):
            d.update(runtime=70, vote_average=6.2, vote_count=1000, release_date="1980-01-01")

        assert reasons_for(edge) == []
        assert reasons_for(lambda d: d.update(release_date="2026-03-01")) == []

    def test_blacklist(self):
        assert reasons_for(lambda d: None, GateRules(blacklist_tmdb_ids={861})) == [Reason.BLACKLISTED]

    def test_franchise_collection(self):
        def coll(d):
            d["belongs_to_collection"] = {"id": 8945, "name": "Mad Max Collection"}

        rules = GateRules(franchise_denylist=FranchiseDenylist(collection_ids={8945}))
        assert reasons_for(coll, rules) == [Reason.FRANCHISE_COLLECTION]
        assert reasons_for(lambda d: None, rules) == []  # null collection is fine

    def test_franchise_company(self):
        rules = GateRules(franchise_denylist=FranchiseDenylist(company_ids={420}))
        assert reasons_for(lambda d: d["production_companies"].append({"id": 420}), rules) == [Reason.FRANCHISE_COMPANY]

    def test_franchise_keyword(self):
        rules = GateRules(franchise_denylist=FranchiseDenylist(keyword_ids={180547}))
        assert reasons_for(lambda d: d["keywords"]["keywords"].append({"id": 180547}), rules) == [
            Reason.FRANCHISE_KEYWORD
        ]

    def test_franchise_title_regex_on_title_and_original_title(self):
        rules = GateRules(franchise_denylist=FranchiseDenylist(title_patterns=[r"\bstar\s+wars\b"]))
        assert reasons_for(lambda d: d.update(title="Star Wars: A New Hope"), rules) == [Reason.FRANCHISE_TITLE]
        assert reasons_for(lambda d: d.update(original_title="STAR WARS"), rules) == [Reason.FRANCHISE_TITLE]
        assert reasons_for(lambda d: None, rules) == []

    def test_bad_regex_fails_at_construction(self):
        with pytest.raises(Exception):
            FranchiseDenylist(title_patterns=["("])

    def test_thresholds_are_parameters(self):
        rules = GateRules(
            min_runtime=120,
            min_vote_average=7.5,
            min_vote_count=8000,
            languages={"en", "fr"},
            excluded_genre_ids={878},
            year_min=1995,
            year_max=2000,
        )
        assert evaluate(base_details(), rules) == [
            Reason.RUNTIME_SHORT,
            Reason.RATING_LOW,
            Reason.VOTES_LOW,
            Reason.GENRE_EXCLUDED,
            Reason.YEAR_OUT_OF_RANGE,
        ]

    def test_all_failures_listed_first_is_reason(self):
        def many(d):
            d.update(runtime=50, original_language="ja", adult=True)

        assert reasons_for(many) == [Reason.RUNTIME_SHORT, Reason.LANGUAGE, Reason.ADULT]

    def test_reason_codes_are_stable_strings(self):
        assert Reason.NO_MATCH.value == "no_match"
        assert len({r.value for r in Reason}) == len(Reason)


# --------------------------------------------------------------------------- cache


class TestCache:
    async def test_second_check_hits_the_cache(self, make_gate, fake):
        gate = make_gate()
        first = await gate.check("Total Recall", 1990)
        n = len(fake.requests)
        second = await make_gate().check("Total Recall", 1990)
        assert second == first and len(fake.requests) == n

    async def test_details_expire_after_7_days_resolution_after_30(self, make_gate, fake, clock):
        await make_gate().check("Total Recall", 1990)
        assert (len(fake.search_calls()), len(fake.detail_calls())) == (1, 1)

        clock.t += 7 * DAY - 1
        await make_gate().check("Total Recall", 1990)
        assert (len(fake.search_calls()), len(fake.detail_calls())) == (1, 1)

        clock.t += 1  # details are now 7 days old
        await make_gate().check("Total Recall", 1990)
        assert (len(fake.search_calls()), len(fake.detail_calls())) == (1, 2)

        clock.t = T0 + 30 * DAY  # resolution is 30 days old (details too: fetched on day 7)
        await make_gate().check("Total Recall", 1990)
        assert (len(fake.search_calls()), len(fake.detail_calls())) == (2, 3)

    async def test_fresh_details_reflect_new_vote_counts(self, make_gate, fake, clock):
        assert isinstance(await make_gate().check("Total Recall", 1990), Candidate)
        fake.movies[861]["vote_count"] = 10
        assert isinstance(await make_gate().check("Total Recall", 1990), Candidate)  # cached
        clock.t += 7 * DAY
        result = await make_gate().check("Total Recall", 1990)
        assert isinstance(result, Rejection) and result.reason is Reason.VOTES_LOW

    async def test_no_match_is_cached_for_7_days(self, make_gate, fake, clock):
        await make_gate().resolve("Modern", 2014)
        n = len(fake.requests)
        clock.t += 7 * DAY - 1
        assert await make_gate().resolve("Modern", 2014) is None
        assert len(fake.requests) == n
        clock.t += 1
        await make_gate().resolve("Modern", 2014)
        assert len(fake.requests) == n + 4

    async def test_credits_and_base_details_are_cached_separately(self, make_gate, fake):
        await make_gate().check("Total Recall", 1990)
        result = await make_gate(GateRules(include_credits=True)).check("Total Recall", 1990)
        assert result.director_ids == (10491,)
        assert fake.detail_calls() == [861, 861]
        with db.session() as conn:
            cached = conn.execute("SELECT details_json FROM tmdb_cache WHERE key = 'details:861:credits'").fetchone()
        assert "Editor" not in cached[0] and "cast" not in cached[0]  # only directors kept

    async def test_cache_key_uses_normalised_title(self, make_gate, fake):
        await make_gate().resolve("Total Recall", 1990)
        n = len(fake.requests)
        assert await make_gate().resolve("TOTAL RECALL!", 1990) == 861
        assert len(fake.requests) == n

    async def test_errors_are_not_cached(self, make_gate, fake):
        fake.script = [httpx.Response(503)] * g.MAX_ATTEMPTS
        with pytest.raises(TMDBError):
            await make_gate().resolve("Total Recall", 1990)
        assert await make_gate().resolve("Total Recall", 1990) == 861
        with db.session() as conn:
            assert conn.execute("SELECT COUNT(*) FROM tmdb_cache").fetchone()[0] == 1

    async def test_concurrent_checks_share_the_cache_safely(self, make_gate):
        gate = make_gate()
        titles = [("Total Recall", 1990), ("Total Recall", 2012), ("The Thing", 1982), ("The Thing", 2011)] * 5
        results = await asyncio.gather(*(gate.check(t, y) for t, y in titles))
        assert {r.tmdb_id for r in results} == {861, 64635, 1091, 60935}


# --------------------------------------------------------------------------- HTTP


class TestRetries:
    async def test_429_honours_retry_after(self, make_gate, fake, sleeps):
        fake.script = [httpx.Response(429, headers={"Retry-After": "3"})]
        assert await make_gate().resolve("Total Recall", 1990) == 861
        assert sleeps == [3.0]

    async def test_429_retry_after_is_capped(self, make_gate, fake, sleeps):
        fake.script = [httpx.Response(429, headers={"Retry-After": "86400"})]
        await make_gate().resolve("Total Recall", 1990)
        assert sleeps == [g.MAX_RETRY_AFTER_S]

    @pytest.mark.parametrize("header", [None, "Wed, 21 Oct 2015 07:28:00 GMT", "soon", "-5", "nan"])
    async def test_429_without_usable_retry_after_backs_off(self, make_gate, fake, sleeps, header):
        headers = {"Retry-After": header} if header else {}
        fake.script = [httpx.Response(429, headers=headers)] * 2
        await make_gate().resolve("Total Recall", 1990)
        assert sleeps == [g.BACKOFF_S[0], g.BACKOFF_S[1]]

    async def test_429_exhausted_raises_rate_limit(self, make_gate, fake, sleeps):
        fake.script = [httpx.Response(429, headers={"Retry-After": "1"})] * g.MAX_ATTEMPTS
        with pytest.raises(TMDBError) as e:
            await make_gate().resolve("Total Recall", 1990)
        assert (e.value.category, e.value.status_code) == ("provider_rate_limit", 429)
        assert len(fake.requests) == g.MAX_ATTEMPTS and len(sleeps) == g.MAX_ATTEMPTS - 1

    async def test_5xx_and_timeouts_retry_with_backoff(self, make_gate, fake, sleeps):
        fake.script = [httpx.Response(502), httpx.ReadTimeout("t"), httpx.ConnectError("c")]
        assert await make_gate().resolve("Total Recall", 1990) == 861
        assert sleeps == list(g.BACKOFF_S)

    async def test_timeout_exhausted(self, make_gate, fake):
        fake.script = [httpx.ReadTimeout("t")] * g.MAX_ATTEMPTS
        with pytest.raises(TMDBError) as e:
            await make_gate().check("Total Recall", 1990)
        assert e.value.category == "timeout" and e.value.status_code is None

    @pytest.mark.parametrize(
        "status,category", [(401, "provider_auth"), (403, "provider_auth"), (400, "provider_http_4xx")]
    )
    async def test_client_errors_do_not_retry(self, make_gate, fake, sleeps, status, category):
        fake.script = [httpx.Response(status)]
        with pytest.raises(TMDBError) as e:
            await make_gate().check("Total Recall", 1990)
        assert (e.value.category, e.value.status_code) == (category, status)
        assert len(fake.requests) == 1 and sleeps == []

    async def test_missing_token_is_auth_error(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "TMDB_READ_ACCESS_TOKEN", None)
        with pytest.raises(TMDBError) as e:
            TMDBClient()
        assert e.value.category == "provider_auth"

    async def test_token_comes_from_settings_and_is_sent_as_bearer(self, monkeypatch, fake):
        from app.core.config import settings

        monkeypatch.setattr(settings, "TMDB_READ_ACCESS_TOKEN", "from-env")
        async with TMDBClient(transport=fake.transport) as client:
            await client.get("/movie/861")
        assert fake.requests[0].headers["Authorization"] == "Bearer from-env"
        assert "from-env" not in str(fake.requests[0].url)

    async def test_concurrency_is_limited(self, make_gate, fake):
        in_flight = peak = 0

        async def handler(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(200, json={"results": []})

        async with TMDBClient(TOKEN, transport=httpx.MockTransport(handler), concurrency=8) as client:
            await asyncio.gather(*(client.get("/search/movie", {"query": str(i)}) for i in range(30)))
        assert peak == 8

    async def test_logs_and_errors_never_carry_provider_text_or_token(self, make_gate, fake, caplog):
        body = {"status_message": "Invalid API key: SECRET-PROVIDER-TEXT"}
        fake.script = [httpx.Response(503, json=body)] * g.MAX_ATTEMPTS
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(TMDBError) as e:
                await make_gate().resolve("Total Recall", 1990)
        text = caplog.text + str(e.value) + repr(e.value)
        assert "SECRET-PROVIDER-TEXT" not in text and TOKEN not in text and "api.themoviedb.org" not in text
        assert "TMDBError (HTTP 503)" in caplog.text
