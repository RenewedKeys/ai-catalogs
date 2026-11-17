"""CuratedLLMService through the real openai SDK against a fake transport (no live LLM)."""

import glob
import json
import logging

import httpx2
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.curation.budget import Budget, BudgetExceeded, BudgetLimits
from app.curation.llm import (
    MAX_TOKENS_CEILING,
    CuratedLLMService,
    LLMError,
    LLMSettings,
    extract_rows,
    validate_rows,
)
from app.store import db
from tests.unit.curation.llm_fake import API_KEY_SENTINEL, BODY_SENTINEL, FakeLLM, completion, error, movies_json


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://llm.invalid/api/v1")


def service(fake: FakeLLM, per_run=20, per_day=100, **llm) -> CuratedLLMService:
    budget = Budget(BudgetLimits(max_model_calls_per_run=per_run, max_model_calls_per_day=per_day))

    async def no_sleep(_):
        return None

    return CuratedLLMService(
        LLMSettings(model="vendor/model-a", **llm), budget, http_client=fake.client(), sleep=no_sleep
    )


def ledger():
    with db.session() as conn:
        return [dict(r) for r in conn.execute("SELECT kind, status, tokens_in, tokens_out FROM llm_ledger ORDER BY id")]


async def test_strict_schema_happy_path():
    fake = FakeLLM([completion(movies_json(5))])
    llm = service(fake, temperature=0.3, max_tokens=4000, timeout_s=30)
    result = await llm.generate("PROMPT TEXT", 5)
    assert [m.title for m in result.movies] == [f"Title {i}" for i in range(5)]
    assert result.mode == "json_schema" and result.attempts == 1
    (req,) = fake.requests
    body = req["body"]
    assert body["model"] == "vendor/model-a"
    assert body["temperature"] == 0.3 and body["max_tokens"] == 4000
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["messages"][1] == {"role": "user", "content": "PROMPT TEXT"}
    assert "exactly 5 films" in body["messages"][0]["content"]
    assert req["url"].startswith("https://llm.invalid/api/v1/")
    assert ledger() == [{"kind": "generate", "status": "done", "tokens_in": 10, "tokens_out": 20}]


async def test_temperature_not_sent_when_unset():
    fake = FakeLLM([completion(movies_json(1))])
    await service(fake).generate("p", 1)
    assert "temperature" not in fake.requests[0]["body"]
    assert fake.requests[0]["body"]["max_tokens"] == 8000


def test_settings_limits():
    assert LLMSettings.from_definition({"max_tokens": MAX_TOKENS_CEILING}).max_tokens == MAX_TOKENS_CEILING
    for bad in ({"max_tokens": MAX_TOKENS_CEILING + 1}, {"temperature": 2.5}, {"timeout_s": 0}):
        with pytest.raises(ValidationError):
            LLMSettings.from_definition(bad)
    assert LLMSettings.from_definition({}).model == settings.DEFAULT_MODEL
    assert LLMSettings.from_definition({}).timeout_s == 120


@pytest.mark.parametrize(
    "status,message",
    [
        (400, "response_format json_schema is not supported for this model"),
        (422, "Structured output unavailable"),
        (404, "No endpoints found that can handle the requested parameters."),
    ],
)
async def test_downgrade_on_capability_error_and_remember(status, message):
    fake = FakeLLM([error(status, message), completion(movies_json(3))])
    result = await service(fake).generate("p", 3)
    assert fake.modes() == ["json_schema", "json_object"]
    assert result.mode == "json_object" and result.attempts == 2
    assert [r["kind"] for r in ledger()] == ["generate", "downgrade"]

    # A second run (new service, as in another process) starts from the remembered mode.
    fake2 = FakeLLM([completion(movies_json(3))])
    await service(fake2).generate("p", 3)
    assert fake2.modes() == ["json_object"]


async def test_downgrade_to_text_with_tolerant_extraction():
    prose = "Sure! Here you go:\n```json\n" + movies_json(2) + "\n```\nEnjoy."
    fake = FakeLLM(
        [
            error(400, "response_format not supported"),
            error(400, "json_object response_format unsupported"),
            completion(prose),
        ]
    )
    result = await service(fake).generate("p", 2)
    assert fake.modes() == ["json_schema", "json_object", "text"]
    assert result.mode == "text" and len(result.movies) == 2


async def test_remembered_mode_expires_after_24h(monkeypatch):
    now = {"t": 1_767_225_600.0}
    monkeypatch.setattr(db, "clock", lambda: now["t"])
    await service(FakeLLM([error(400, "json_schema not supported"), completion(movies_json(1))])).generate("p", 1)
    now["t"] += 24 * 3600 + 1
    fake = FakeLLM([completion(movies_json(1))])
    await service(fake).generate("p", 1)
    assert fake.modes() == ["json_schema"]


async def test_plain_400_does_not_downgrade():
    fake = FakeLLM([error(400, "invalid model id")])
    with pytest.raises(LLMError) as e:
        await service(fake).generate("p", 1)
    assert e.value.category == "provider_http_4xx" and e.value.status_code == 400
    assert fake.modes() == ["json_schema"]
    assert ledger() == [{"kind": "generate", "status": "failed", "tokens_in": 0, "tokens_out": 0}]


async def test_404_model_not_found_does_not_downgrade():
    fake = FakeLLM([error(404, "model does not exist")])
    with pytest.raises(LLMError):
        await service(fake).generate("p", 1)
    assert len(fake.requests) == 1


async def test_auth_error_fails_with_hint_and_no_retry():
    fake = FakeLLM([error(401, "invalid api key")])
    with pytest.raises(LLMError) as e:
        await service(fake).generate("p", 1)
    assert e.value.category == "provider_auth" and len(fake.requests) == 1
    assert "OpenRouter key" in e.value.hint and "llm.invalid" in e.value.hint
    assert API_KEY_SENTINEL not in str(e.value) and "SENTINEL" not in str(e.value)


@pytest.mark.parametrize("status,category", [(429, "provider_rate_limit"), (503, "provider_http_5xx")])
async def test_transient_errors_retry_twice_through_the_budget(status, category):
    fake = FakeLLM([error(status, "busy"), error(status, "busy"), completion(movies_json(1))])
    result = await service(fake).generate("p", 1)
    assert result.attempts == 3 and len(fake.requests) == 3
    assert [r["kind"] for r in ledger()] == ["generate", "retry", "retry"]
    assert [r["status"] for r in ledger()] == ["failed", "failed", "done"]

    fake = FakeLLM([error(status, "busy")])
    with pytest.raises(LLMError) as e:
        await service(fake).generate("p", 1)
    assert e.value.category == category and len(fake.requests) == 3  # 1 + 2 retries, no SDK retries


async def test_timeout_is_transient():
    fake = FakeLLM([httpx2.ReadTimeout("slow"), completion(movies_json(1))])
    result = await service(fake).generate("p", 1)
    assert result.attempts == 2


async def test_budget_refuses_mid_retry():
    fake = FakeLLM([error(503, "busy")])
    with pytest.raises(BudgetExceeded):
        await service(fake, per_run=2).generate("p", 1)
    assert len(fake.requests) == 2 and len(ledger()) == 2


async def test_budget_counts_downgrade_attempts():
    fake = FakeLLM([error(400, "json_schema not supported"), completion(movies_json(1))])
    with pytest.raises(BudgetExceeded):
        await service(fake, per_run=1).generate("p", 1)
    assert len(fake.requests) == 1


async def test_finish_reason_length_with_empty_content():
    usage = {
        "prompt_tokens": 50,
        "completion_tokens": 8000,
        "total_tokens": 8050,
        "completion_tokens_details": {"reasoning_tokens": 7999},
    }
    fake = FakeLLM([completion("", "length", usage)])
    with pytest.raises(LLMError) as e:
        await service(fake).generate("p", 100)
    assert e.value.category == "output_truncated"
    assert "max_tokens (8000)" in str(e.value) and "7999" in str(e.value)
    assert len(fake.requests) == 1
    assert ledger()[0]["tokens_out"] == 8000


async def test_truncated_reply_is_salvaged():
    cut = movies_json(4)[:-30]  # cut off mid-row
    result = await service(FakeLLM([completion(cut, "length")])).generate("p", 4)
    assert 1 <= len(result.movies) < 4 and result.finish_reason == "length"


async def test_unparseable_reply_is_output_invalid():
    with pytest.raises(LLMError) as e:
        await service(FakeLLM([completion("no json here")])).generate("p", 1)
    assert e.value.category == "output_invalid"


def test_rows_are_validated_individually_and_cleaned():
    rows = [
        {"title": "Title\x07 A\n", "year": "1999", "category": "alpha"},
        {"title": "", "year": 2000},
        {"title": "Title B", "year": "unknown"},
        {"title": "Title C", "year": 1500},
        {"title": 42, "year": 2000},
        {"title": "T" * 500, "year": 2001, "category": None},
        "not a row",
        {"title": "Title D", "year": True},
    ]
    movies, dropped = validate_rows(rows)
    assert [(m.title[:10], m.year, m.category) for m in movies] == [("Title A", 1999, "alpha"), ("T" * 10, 2001, "")]
    assert len(movies[1].title) == 200
    assert dropped == 6


def test_extract_rows():
    assert extract_rows(movies_json(2), tolerant=False) and len(extract_rows(movies_json(2), tolerant=False)) == 2
    assert extract_rows("prose " + movies_json(2), tolerant=False) is None
    assert len(extract_rows("prose " + movies_json(2), tolerant=True)) == 2
    assert extract_rows(json.dumps({"films": [{"title": "Title A", "year": 1}]}), tolerant=False)
    assert len(extract_rows('[{"title": "Title A", "year": 2000}]', tolerant=True)) == 1


async def test_missing_key_is_config_error(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    with pytest.raises(LLMError) as e:
        await service(FakeLLM([completion(movies_json(1))])).generate("p", 1)
    assert e.value.category == "config"


async def test_no_secret_or_provider_text_in_logs_errors_or_db(caplog, _temporary_database):
    caplog.set_level(logging.DEBUG)
    errors = []
    for reply in (
        [error(401, "bad key " + API_KEY_SENTINEL)],
        [error(400, "weird " + API_KEY_SENTINEL)],
        [error(503, "x")],
    ):
        with pytest.raises(LLMError) as e:
            await service(FakeLLM(reply)).generate("p", 1)
        errors.append(e.value)
    await service(FakeLLM([error(400, "json_schema not supported"), completion(movies_json(1))])).generate("p", 1)

    haystacks = [caplog.text] + [str(e) for e in errors] + [repr(e.args) for e in errors]
    for path in glob.glob(_temporary_database + "*"):  # the database and its -wal / -shm files
        with open(path, "rb") as f:
            haystacks.append(f.read().decode("latin-1"))
    for text in haystacks:
        assert API_KEY_SENTINEL not in text
        assert BODY_SENTINEL not in text
        assert "PROMPT" not in text
    for e in errors:
        assert e.__suppress_context__  # raised "from None": tracebacks do not chain the SDK error
