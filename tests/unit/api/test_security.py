"""
Security regression tests for milestone 1 (PLAN.md, sections 3 and 10):

- no unauthenticated response contains a server-side secret, with the legacy surface on or off
- the configure flow never renders credentials, and a stored LLM key is never sent to a
  base URL other than the one it was stored for
- the legacy surface is not mounted by default
- CORS headers are only sent by the Stremio sub-app
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

    app = create_app(enable_legacy=enable_legacy)
    client = TestClient(app, follow_redirects=False)
    blob = _user_blob()
    paths = _get_paths(app, blob) + ["/", "/configure", f"/configure?config={blob}", "/does-not-exist"]
    assert "/healthz" in paths
    if enable_legacy:
        assert f"/config/{blob}/preview" in paths
        assert f"/config/{blob}/manifest.json" in paths

    for path in paths:
        response = client.get(path, headers={"Origin": "https://example.org"})
        _assert_clean(response, secrets, path.replace(blob, "<blob>"))


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
