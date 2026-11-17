"""
/ops (PLAN.md section 10): bearer token, 404 when unset, enqueue-only webhooks with run
admission, the status page, and CORS isolation from the Stremio catch-all.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.core.config import settings
from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, publish_snapshot

TOKEN = "ops-test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = {"Origin": "https://elsewhere.example"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "OPS_TOKEN", TOKEN)
    return TestClient(create_app(enable_legacy=False))


def add_catalog(catalog_id="example", state="enabled", **extra):
    definition = {"prompt": "Suggest films.", **extra}
    with db.session() as conn, db.transaction(conn):
        rev = accept_definition(conn, catalog_id, "Example <b>list</b>", definition, state=state)
        return rev


def test_unset_token_hides_every_ops_path(monkeypatch):
    monkeypatch.setattr(settings, "OPS_TOKEN", None)
    client = TestClient(create_app(enable_legacy=False))
    for method, path in [("GET", "/ops/status"), ("POST", "/ops/catalogs/example/refresh"), ("GET", "/ops/nope")]:
        r = client.request(method, path, headers={**AUTH, **ORIGIN})
        assert r.status_code == 404
        assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": ("Bearer " + "\u00e9" * 8).encode("latin-1")},  # non-ASCII: 401, not a 500
    ],
)
def test_wrong_or_missing_token_is_401(client, headers):
    add_catalog()
    for method, path in [("GET", "/ops/status"), ("POST", "/ops/catalogs/example/refresh")]:
        r = client.request(method, path, headers=headers)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
    with db.session() as conn:  # nothing queued
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_token_in_the_query_string_is_not_accepted(client):
    assert client.get(f"/ops/status?token={TOKEN}").status_code == 401


def test_refresh_and_dry_run_only_enqueue_with_admission(client):
    add_catalog(max_runs_per_day=1)
    r = client.post("/ops/catalogs/example/refresh", headers=AUTH)
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["kind"] == "refresh"
    job_id = r.json()["job_id"]
    r = client.post("/ops/catalogs/example/dry-run", headers=AUTH)
    assert r.status_code == 409 and r.json()["job_id"] == job_id  # one open job per catalog
    with db.session() as conn:
        job = conn.execute("SELECT state, trigger FROM jobs").fetchone()
        assert (job["state"], job["trigger"]) == ("pending", "ops")  # queued, not run
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        conn.execute("UPDATE jobs SET state = 'done'")
        conn.execute(
            "INSERT INTO runs (job_id, catalog_id, config_rev, epoch, trigger, started_at, status) "
            "SELECT ?, catalog_id, config_rev, epoch, 'ops', ?, 'ok' FROM catalog_defs",
            (job_id, db.now_ms()),
        )
    r = client.post("/ops/catalogs/example/refresh", headers=AUTH)
    assert r.status_code == 429 and r.json()["status"] == "daily_limit"
    r = client.post("/ops/catalogs/example/dry-run", headers=AUTH)  # dry runs do not count
    assert r.status_code == 202 and r.json()["kind"] == "dry_run"


def test_unknown_or_disabled_catalog_is_404(client):
    add_catalog("off", state="disabled")
    assert client.post("/ops/catalogs/missing/refresh", headers=AUTH).status_code == 404
    assert client.post("/ops/catalogs/off/refresh", headers=AUTH).status_code == 404


def test_status_json_and_html(client):
    rev = add_catalog(schedule={"cron": "0 6 * * *", "timezone": "Europe/Berlin"})
    with db.session() as conn:
        with db.transaction(conn):
            publish_snapshot(conn, "example", [SnapshotItem("tt0000001", "Film 1", 2000, 1)], config_rev=rev)
            conn.execute(
                "INSERT INTO runs (catalog_id, config_rev, epoch, trigger, started_at, status, stage_counts_json, "
                "reject_reasons_json, error_category, error_status) "
                "VALUES ('example', ?, 0, 'schedule', ?, 'failed', ?, ?, 'provider_http_5xx', 503)",
                (rev, db.now_ms(), json.dumps({"suggested": 160}), json.dumps({"<script>": 3})),
            )
            conn.execute(
                "INSERT INTO definition_errors (catalog_id, file, error, at) VALUES ('bad', 'bad.yaml', ?, 0)",
                ("prompt: <img src=x>",),
            )
        from app.curation.schedule import sync_schedules

        sync_schedules(conn)
    data = client.get("/ops/status", headers=AUTH).json()
    c = data["catalogs"][0]
    assert c["catalog_id"] == "example" and c["active_snapshot"]["items"] == 1
    assert c["schedule"] == {"cron": "0 6 * * *", "timezone": "Europe/Berlin"} and c["next_run_at"]
    run = c["recent_runs"][0]
    assert run["error_category"] == "provider_http_5xx" and run["stage_counts"] == {"suggested": 160}
    assert data["definition_errors"][0]["file"] == "bad.yaml"
    assert data["worker"]["lease_held"] is False
    assert data["budget"]["model_calls_limit"] == settings.MAX_MODEL_CALLS_PER_DAY
    assert "prompt" not in json.dumps(c)  # definition content is never shown

    r = client.get("/ops/status", headers={**AUTH, "Accept": "text/html"})
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert "<script>" not in r.text and "<img" not in r.text and "<b>list" not in r.text
    assert "&lt;script&gt;" in r.text and "provider_http_5xx" in r.text
    assert client.get("/ops/status?format=html", headers=AUTH).headers["content-type"].startswith("text/html")


# --------------------------------------------------------------------------- CORS isolation


@pytest.mark.parametrize(
    "method, path, headers, expected",
    [
        ("GET", "/ops/status", AUTH, 200),  # authorised
        ("POST", "/ops/catalogs/example/refresh", AUTH, 202),  # authorised
        ("GET", "/ops/status", {}, 401),  # unauthorised
        ("POST", "/ops/catalogs/example/refresh", {"Authorization": "Bearer nope"}, 401),
        ("GET", "/ops/does-not-exist", AUTH, 404),  # missing path, authorised
        ("GET", "/ops/does-not-exist", {}, 401),  # missing path, unauthorised
    ],
)
def test_ops_responses_carry_no_cors_headers(client, method, path, headers, expected):
    add_catalog()
    r = client.request(method, path, headers={**headers, **ORIGIN})
    assert r.status_code == expected
    assert not any(h.startswith("access-control-") for h in r.headers)


@pytest.mark.parametrize("path", ["/ops/status", "/ops/catalogs/example/refresh", "/ops/does-not-exist"])
def test_ops_preflight_is_not_answered_by_stremio_cors(client, path):
    r = client.options(
        path,
        headers={**ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"},
    )
    assert r.status_code in (401, 404, 405)
    assert not any(h.startswith("access-control-") for h in r.headers)


def test_stremio_routes_keep_their_cors(client):
    r = client.get("/c/public/manifest.json", headers=ORIGIN)
    assert r.headers.get("access-control-allow-origin") == "*"
