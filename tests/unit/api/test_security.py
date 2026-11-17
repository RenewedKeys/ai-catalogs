"""
Security regression tests for milestone 1 (PLAN.md, sections 3 and 10):

- no unauthenticated response contains a server-side secret, with the legacy surface on or off
- the configure flow never renders credentials, and a stored LLM key is never sent to a
  base URL other than the one it was stored for
- the legacy surface is not mounted by default
- CORS headers are only sent by the Stremio sub-app
- authorised /ops responses (status JSON and HTML, webhooks) carry no secret either
- logged-in /admin pages and actions (milestone 8) carry no secret either
"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

from app.api import create_app, mask_url
from app.core.config import Settings, settings
from app.services.encryption import encryption_service

SENTINELS = {
    "OPENAI_API_KEY": "sk-SENTINEL-openai-0123456789",
    "TMDB_READ_ACCESS_TOKEN": "SENTINEL-tmdb-0123456789",
    "RPDB_API_KEY": "SENTINEL-rpdb-0123456789",
    "ENCRYPTION_KEY": "SENTINEL-encryption-0123456789",
    "ADDON_ACCESS_TOKEN": "SENTINELaddontoken0123456789",
    "OPS_TOKEN": "SENTINEL-ops-token-0123456789",
    "SESSION_SECRET": "SENTINEL-session-secret-0123456789abcdef",
    "ADMIN_PASSWORD": "SENTINEL-admin-password-0123456789",
}
# Credentials a user typed into the form themselves; they live inside the encrypted config.
USER_SECRETS = {
    "openai_api_key": "sk-USERSECRET-openai-0123456789",
    "tmdb_read_access_token": "USERSECRET-tmdb-0123456789",
    "posterdb_api_key": "USERSECRET-rpdb-0123456789",
}
ENV_BASE_URL = "https://openrouter.ai/api/v1"


@pytest.fixture
def secrets(monkeypatch):
    """Give the settings singleton a recognisable value for every server-side secret."""
    for name, value in SENTINELS.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", ENV_BASE_URL)
    monkeypatch.setattr(encryption_service, "password", SENTINELS["ENCRYPTION_KEY"].encode())
    return list(SENTINELS.values()) + list(USER_SECRETS.values())


@pytest.fixture
def no_outbound(monkeypatch):
    """Replace the connection test in /save-config; records the Config it was given."""
    mock = AsyncMock(return_value={})
    monkeypatch.setattr("app.api.web.ConfigValidationService.validate_config", mock)
    return mock


def _user_blob() -> str:
    return encryption_service.encrypt(
        json.dumps({**USER_SECRETS, "openai_base_url": ENV_BASE_URL, "model_name": "m", "use_posterdb": True})
    )


def _get_paths(app, blob: str) -> list[str]:
    """Every GET route of the app and its mounted sub-apps, with path parameters filled in."""
    fill = {
        "config": blob,
        "adult": "0",
        "content_type": "movie",
        "content_type_extra": "movie",
        "catalog_id": "trending_movie",
        "skip": "0",
        "search": "heist",
        "token": SENTINELS["ADDON_ACCESS_TOKEN"],
        "extra": "skip=0",
        "snapshot_id": "1",
        "name": "admin.js",
    }
    paths = []

    def walk(routes, prefix=""):
        for route in routes:
            if isinstance(route, Mount) and hasattr(route.app, "routes"):
                walk(route.app.routes, prefix + route.path)
            elif hasattr(route, "original_router"):
                # FastAPI keeps an included router as one lazy entry. The asserts in the
                # test below fail loudly if this stops finding the legacy routes.
                walk(route.original_router.routes, prefix)
            elif isinstance(route, Route) and "GET" in (route.methods or ()):
                path = prefix + route.path
                for name, value in fill.items():
                    path = path.replace("{" + name + "}", value)
                assert "{" not in path, f"unfilled path parameter in {path}"
                paths.append(path)

    walk(app.routes)
    return paths


def _load_fixture_catalog():
    """Give the curated routes something to serve, so the sweep reads real responses."""
    from app.store.catalogs import SnapshotItem, accept_definition, publish_snapshot
    from app.store.db import session, transaction

    with session() as conn, transaction(conn):
        rev = accept_definition(conn, "trending_movie", "Sweep", {"k": 1})
        publish_snapshot(conn, "trending_movie", [SnapshotItem("tt0000001", "A Film", 1999, 1)], config_rev=rev)


def _assert_clean(response, secrets, where):
    haystack = response.text + "\n" + "\n".join(f"{k}: {v}" for k, v in response.headers.items())
    for secret in secrets:
        assert secret not in haystack, f"secret leaked by {where}"


@pytest.mark.parametrize("enable_legacy", [False, True])
def test_no_get_route_reveals_a_secret(enable_legacy, secrets, monkeypatch):
    async def fake_catalog(*args, **kwargs):
        return {"metas": []}

    monkeypatch.setattr("app.api.stremio._cached_catalog", fake_catalog)
    monkeypatch.setattr("app.api.stremio._process_catalog_request", AsyncMock(return_value={"metas": []}))

    _load_fixture_catalog()
    app = create_app(enable_legacy=enable_legacy)
    client = TestClient(app, follow_redirects=False)
    blob = _user_blob()
    token = SENTINELS["ADDON_ACCESS_TOKEN"]
    paths = _get_paths(app, blob) + ["/", "/configure", f"/configure?config={blob}", "/does-not-exist"]
    assert "/healthz" in paths
    assert f"/c/{token}/manifest.json" in paths
    assert client.get(f"/c/{token}/catalog/movie/trending_movie.json").json()["metas"][0]["id"] == "tt0000001"
    if enable_legacy:
        assert f"/config/{blob}/preview" in paths
        assert f"/config/{blob}/manifest.json" in paths

    for path in paths:
        response = client.get(path, headers={"Origin": "https://example.org"})
        # The token is in the request path; it must not come back in the response.
        _assert_clean(response, secrets, path.replace(blob, "<blob>").replace(token, "<token>"))


def test_configure_page_offers_to_keep_stored_keys_without_showing_them(secrets):
    client = TestClient(create_app(enable_legacy=True))

    fresh = client.get("/configure")
    assert fresh.status_code == 200
    assert "Leave blank to keep the stored key" in fresh.text
    _assert_clean(fresh, secrets, "/configure")

    blob = _user_blob()
    editing = client.get(f"/configure?config={blob}")
    assert editing.status_code == 200
    assert 'name="existing_config"' in editing.text
    _assert_clean(editing, secrets, "/configure?config=")


def test_configure_page_rejects_a_bad_config(secrets):
    client = TestClient(create_app(enable_legacy=True))
    assert client.get("/configure?config=not-a-config").status_code == 400


def test_blank_keys_fall_back_to_env_and_round_trip_stays_clean(secrets, no_outbound):
    client = TestClient(create_app(enable_legacy=True))

    saved = client.post("/save-config", data={"openai_base_url": ENV_BASE_URL, "model_name": "m"})
    assert saved.status_code == 200, saved.text
    _assert_clean(saved, secrets, "POST /save-config")

    used = no_outbound.call_args[0][0]
    assert used.openai_api_key == SENTINELS["OPENAI_API_KEY"]
    assert used.tmdb_read_access_token == SENTINELS["TMDB_READ_ACCESS_TOKEN"]

    # The new config now contains the env keys; nothing that renders it may show them.
    blob = saved.json()["manifest_url"].split("/config/")[1].split("/")[0]
    for path in (f"/configure?config={blob}", f"/config/{blob}/preview", f"/config/{blob}/manifest.json"):
        response = client.get(path)
        assert response.status_code == 200
        _assert_clean(response, secrets, path.replace(blob, "<blob>"))


def test_env_key_is_not_sent_to_another_base_url(secrets, no_outbound):
    client = TestClient(create_app(enable_legacy=True))
    response = client.post("/save-config", data={"openai_base_url": "https://attacker.example/v1", "model_name": "m"})
    assert response.status_code == 400
    no_outbound.assert_not_called()
    _assert_clean(response, secrets, "POST /save-config")


def test_stored_key_is_not_sent_to_another_base_url(secrets, no_outbound):
    """Editing a config (whose key the editor cannot read) must not redirect that key elsewhere."""
    client = TestClient(create_app(enable_legacy=True))
    response = client.post(
        "/save-config",
        data={"openai_base_url": "https://attacker.example/v1", "model_name": "m", "existing_config": _user_blob()},
    )
    assert response.status_code == 400
    no_outbound.assert_not_called()


def test_editing_keeps_stored_keys_for_the_same_provider(secrets, no_outbound):
    client = TestClient(create_app(enable_legacy=True))
    response = client.post(
        "/save-config",
        data={
            "openai_base_url": ENV_BASE_URL + "/",
            "model_name": "m",
            "use_posterdb": "on",
            "existing_config": _user_blob(),
        },
    )
    assert response.status_code == 200, response.text
    used = no_outbound.call_args[0][0]
    assert used.openai_api_key == USER_SECRETS["openai_api_key"]
    assert used.tmdb_read_access_token == USER_SECRETS["tmdb_read_access_token"]
    assert used.posterdb_api_key == USER_SECRETS["posterdb_api_key"]
    _assert_clean(response, secrets, "POST /save-config")


def test_env_rpdb_key_is_never_a_fallback(secrets, no_outbound):
    client = TestClient(create_app(enable_legacy=True))
    response = client.post("/save-config", data={"openai_base_url": ENV_BASE_URL, "model_name": "m"})
    assert response.status_code == 200
    assert no_outbound.call_args[0][0].posterdb_api_key is None


def test_validation_errors_are_scrubbed(secrets, monkeypatch):
    leaky = AsyncMock(return_value={"LLM": f"Incorrect API key provided: {SENTINELS['OPENAI_API_KEY']}"})
    monkeypatch.setattr("app.api.web.ConfigValidationService.validate_config", leaky)
    client = TestClient(create_app(enable_legacy=True))
    response = client.post("/save-config", data={"openai_base_url": ENV_BASE_URL, "model_name": "m"})
    assert response.status_code == 400
    _assert_clean(response, secrets, "POST /save-config")


def test_legacy_routes_are_not_mounted_by_default():
    assert Settings.model_fields["ENABLE_LEGACY"].default is False

    client = TestClient(create_app(enable_legacy=False), follow_redirects=False)
    assert client.get("/healthz").json() == {"status": "ok"}
    for path in ("/", "/configure", "/config/x", "/config/x/manifest.json", "/config/x/preview"):
        assert client.get(path).status_code == 404, path
    assert client.post("/save-config", data={}).status_code in (404, 405)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_legacy_requires_an_encryption_key():
    with pytest.raises(ValueError):
        Settings(_env_file=None, ENABLE_LEGACY=True, ENCRYPTION_KEY=None)
    assert Settings(_env_file=None, ENABLE_LEGACY=False, ENCRYPTION_KEY=None).ENABLE_LEGACY is False


def test_cors_only_on_the_stremio_sub_app(secrets):
    client = TestClient(create_app(enable_legacy=True), follow_redirects=False)
    origin = {"Origin": "https://web.stremio.com"}
    blob = _user_blob()

    manifest = client.get(f"/config/{blob}/manifest.json", headers=origin)
    assert manifest.status_code == 200
    assert manifest.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in manifest.headers

    preflight = client.options(
        f"/config/{blob}/manifest.json", headers={**origin, "Access-Control-Request-Method": "GET"}
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"

    refused = client.options(
        f"/config/{blob}/manifest.json", headers={**origin, "Access-Control-Request-Method": "POST"}
    )
    assert refused.status_code == 400

    # Parent routes: the configure UI, the save endpoint and the health check.
    for response in (
        client.get("/configure", headers=origin),
        client.get("/healthz", headers=origin),
        client.get(f"/config/{blob}/preview", headers=origin),
        client.post("/save-config", data={}, headers=origin),
    ):
        assert "access-control-allow-origin" not in response.headers


def test_request_log_masks_credential_path_segments():
    assert mask_url("http://h/config/SECRETBLOB/manifest.json") == "http://h/config/****/manifest.json"
    assert mask_url("http://h/c/TOKEN/catalog/movie/x.json") == "http://h/c/****/catalog/movie/x.json"
    assert mask_url("http://h/configure?config=SECRETBLOB") == "http://h/configure"
    assert mask_url("http://h/healthz") == "http://h/healthz"


def test_catalog_errors_do_not_echo_the_provider_message(secrets, monkeypatch):
    """Provider errors can quote the API key; the 500 detail must stay generic."""

    class Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(f"Incorrect API key provided: {USER_SECRETS['openai_api_key']}")

    monkeypatch.setattr("app.api.stremio.LLMService", Boom)
    client = TestClient(create_app(enable_legacy=True))
    response = client.get(f"/config/{_user_blob()}/catalog/movie/trending_movie.json")
    assert response.status_code == 500
    _assert_clean(response, secrets, "catalog error")


async def test_llm_failures_do_not_log_the_provider_message(secrets, sample_config, caplog):
    """The legacy LLM service swallows provider errors; what it logs must not quote them."""
    from app.services.llm import LLMService

    secret = USER_SECRETS["openai_api_key"]
    service = LLMService(sample_config)
    service._structured_output_supported = lambda: True
    service._try_fallback_completion = AsyncMock(side_effect=RuntimeError(f"Incorrect API key provided: {secret}"))

    for first_error in (RuntimeError(f"bad key {secret}"), AttributeError(f"bad key {secret}")):
        service._try_structured_completion = AsyncMock(side_effect=first_error)
        with caplog.at_level("DEBUG"):
            assert await service.generate_movie_suggestions("heist films", 5) == []

    assert caplog.records, "expected the failures to be logged"
    assert secret not in caplog.text


def test_invalid_config_is_not_logged_verbatim(secrets, caplog):
    """A decryptable config that fails validation must not have its values logged."""
    secret = USER_SECRETS["openai_api_key"]
    blob = encryption_service.encrypt(json.dumps({"openai_api_key": secret, "language": secret}))
    client = TestClient(create_app(enable_legacy=True))
    with caplog.at_level("DEBUG"):
        for path in (f"/configure?config={blob}", f"/config/{blob}/preview", f"/config/{blob}/manifest.json"):
            assert client.get(path).status_code == 400
    assert caplog.records
    assert secret not in caplog.text


def test_routes_added_after_create_app_would_be_shadowed():
    """
    Guard for later milestones: the Stremio sub-app is a catch-all mounted last, so /ops and
    /admin must be registered inside create_app() before it, never added to the app afterwards.
    """
    app = create_app(enable_legacy=False)
    assert app.routes[-1].path == "" and app.routes[-1].app is app.state.stremio_app


def test_authorised_ops_responses_reveal_no_secret(secrets, caplog):
    """The status page after failed runs, with every secret set: only controlled values come back."""
    from app.store.catalogs import accept_definition
    from app.store.db import now_ms, session, transaction

    _load_fixture_catalog()
    with session() as conn, transaction(conn):
        rev = accept_definition(conn, "sweep_two", "Sweep two", {"prompt": "p", "schedule": {"cron": "0 6 * * *"}})
        conn.execute(
            "INSERT INTO runs (catalog_id, config_rev, epoch, trigger, started_at, status, error_category, "
            "error_status) VALUES ('sweep_two', ?, 0, 'schedule', ?, 'failed', 'provider_auth', 401)",
            (rev, now_ms()),
        )
    client = TestClient(create_app(enable_legacy=False))
    auth = {"Authorization": f"Bearer {SENTINELS['OPS_TOKEN']}", "Origin": "https://example.org"}
    responses = [
        client.get("/ops/status", headers=auth),
        client.get("/ops/status", headers={**auth, "Accept": "text/html"}),
        client.post("/ops/catalogs/sweep_two/refresh", headers=auth),
        client.post("/ops/catalogs/sweep_two/dry-run", headers=auth),
        client.post("/ops/catalogs/missing/refresh", headers=auth),
        client.get("/ops/status", headers={"Authorization": "Bearer wrong"}),
    ]
    assert [r.status_code for r in responses] == [200, 200, 202, 409, 404, 401]
    assert "provider_auth" in responses[0].text
    for r in responses:
        _assert_clean(r, secrets, r.request.url.path)
    for secret in secrets:
        assert secret not in caplog.text


def test_admin_editor_reveals_no_secret(secrets, caplog, monkeypatch, tmp_path):
    """
    Logged in to /admin with every secret set (including the session secret and the password
    used to log in): every page, the status JSON, the export, the prompt preview, a validation
    error, a save, a dry run and a refused rollback come back without any secret, and nothing
    is logged with one.
    """
    import re

    from app.store.catalogs import accept_definition
    from app.store.db import now_ms, session, transaction

    monkeypatch.setattr(settings, "ENABLE_EDITOR", True)
    monkeypatch.setattr(settings, "CATALOGS_DIR", str(tmp_path))
    with session() as conn, transaction(conn):
        rev = accept_definition(conn, "sweep_two", "Sweep two", {"prompt": "Suggest {{ request_count }} films."})
        conn.execute("UPDATE catalog_defs SET source = 'editor' WHERE catalog_id = 'sweep_two'")
        conn.execute(
            "INSERT INTO runs (catalog_id, config_rev, epoch, trigger, started_at, status, error_category, "
            "error_status) VALUES ('sweep_two', ?, 0, 'admin', ?, 'failed', 'provider_auth', 401)",
            (rev, now_ms()),
        )
    app = create_app(enable_legacy=False)
    client = TestClient(app, follow_redirects=False)
    origin = {"Origin": "https://example.org"}
    responses = [client.get("/admin/login", headers=origin)]

    def csrf(html):
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    responses.append(client.post("/admin/login", data={"csrf": csrf(responses[0].text), "password": "wrong-one!!!!"}))
    responses.append(
        client.post("/admin/login", data={"csrf": csrf(responses[0].text), "password": SENTINELS["ADMIN_PASSWORD"]})
    )
    assert responses[-1].status_code == 303
    token = csrf(client.get("/admin/").text)
    paths = [p for p in _get_paths(app, "x") if p.startswith("/admin")]
    paths = [p.replace("trending_movie", "sweep_two") for p in paths] + ["/admin/catalogs/sweep_two/export.yaml"]
    assert "/admin/catalogs/sweep_two" in paths and "/admin/static/admin.js" in paths
    responses += [client.get(p, headers=origin) for p in paths]
    form = {"csrf": token, "mode": "form", "base_rev": rev}
    responses += [
        client.post("/admin/catalogs/sweep_two/save", data={**form, "prompt": "{{ nope }}", "action": "save"}),
        client.post(
            "/admin/catalogs/sweep_two/save", data={**form, "prompt": "Films {{ request_count }}", "action": "validate"}
        ),
        client.post("/admin/catalogs/sweep_two/dry-run", data={"csrf": token}),
        client.post("/admin/catalogs/sweep_two/rollback/1", data={"csrf": token}),
        client.post("/admin/catalogs/sweep_two/save", data={**form, "prompt": "x", "action": "save"}, headers=origin),
    ]
    assert [r.status_code for r in responses[-5:]] == [422, 200, 303, 409, 403]
    for r in responses:
        _assert_clean(r, secrets, r.request.url.path)
    for secret in secrets:
        assert secret not in caplog.text
