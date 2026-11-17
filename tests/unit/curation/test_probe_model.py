"""scripts/probe_model.py against a fake transport (the live probe needs a real key)."""

import importlib.util
import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.store import db
from tests.unit.curation.llm_fake import API_KEY_SENTINEL, BODY_SENTINEL, FakeLLM, completion, error, movies_json

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("probe_model", ROOT / "scripts" / "probe_model.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", API_KEY_SENTINEL)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://llm.invalid/api/v1")


def outputs(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]


async def test_probes_plain_and_online_and_recommends_plain(capsys):
    fake = FakeLLM([completion(movies_json(10))])
    code = await probe.run(["--model", "vendor/model-a:online", "--temperature", "0.5"], http_client=fake.client())
    assert code == 0
    out = outputs(capsys)
    assert [o.get("model") for o in out[:2]] == ["vendor/model-a", "vendor/model-a:online"]
    assert all(o["ok"] and o["mode"] == "json_schema" and o["rows"] == 10 for o in out[:2])
    assert out[2] == {"recommended_model": "vendor/model-a"}
    assert all(r["body"]["temperature"] == 0.5 for r in fake.requests)
    with db.session() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM llm_ledger")]
    assert kinds == ["probe", "probe"]


async def test_online_degrading_json_keeps_plain(capsys):
    def reply(body):
        if body["model"].endswith(":online") and body.get("response_format", {}).get("type") == "json_schema":
            return error(400, "response_format not supported")
        return completion(movies_json(10))

    fake = FakeLLM([reply])
    code = await probe.run(["--model", "vendor/model-a"], http_client=fake.client())
    out = outputs(capsys)
    assert code == 0
    assert out[1]["mode"] != "json_schema"
    assert out[2]["recommended_model"] == "vendor/model-a"


async def test_failure_reports_category_only(capsys):
    fake = FakeLLM([error(401, "bad key " + API_KEY_SENTINEL)])
    code = await probe.run(["--model", "vendor/model-a", "--no-online"], http_client=fake.client())
    text = capsys.readouterr().out
    assert code == 1
    assert '"error": "provider_auth"' in text
    assert API_KEY_SENTINEL not in text and BODY_SENTINEL not in text


async def test_missing_key_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    assert await probe.run(["--model", "vendor/model-a"]) == 2


async def test_invalid_temperature_exits_2(capsys):
    assert await probe.run(["--temperature", "3"]) == 2
