"""
Security regression tests (PLAN.md, sections 3 and 10):

- no unauthenticated response contains a server-side secret
- CORS headers are only sent by the Stremio sub-app
- the removed upstream routes stay gone
- authorised /ops responses (status JSON and HTML, webhooks) carry no secret either
- logged-in /configure pages and actions (milestone 8) carry no secret either
"""

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

from app.api import create_app, mask_url
from app.core.config import settings

SENTINELS = {
    "OPENAI_API_KEY": "sk-SENTINEL-openai-0123456789",
    "GEMINI_API_KEY": "AIzaSENTINEL-gemini-0123456789",
    "TMDB_READ_ACCESS_TOKEN": "SENTINEL-tmdb-0123456789",
    "MDBLIST_API_KEY": "SENTINEL-mdblist-0123456789",
    "ADDON_ACCESS_TOKEN": "SENTINELaddontoken0123456789",
    "OPS_TOKEN": "SENTINEL-ops-token-0123456789",
    "SESSION_SECRET": "SENTINEL-session-secret-0123456789abcdef",
    "ADMIN_PASSWORD": "SENTINEL-admin-password-0123456789",
}


@pytest.fixture
def secrets(monkeypatch):
    """Give the settings singleton a recognisable value for every server-side secret."""
    for name, value in SENTINELS.items():
        monkeypatch.setattr(settings, name, value)
    return list(SENTINELS.values())


def _get_paths(app, blob: str = "x") -> list[str]:
    """Every GET route of the app and its mounted sub-apps, with path parameters filled in."""
    fill = {
        "content_type": "movie",
        "catalog_id": "trending_movie",
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
                # test below fail loudly if this stops finding the curated routes.
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


def test_no_get_route_reveals_a_secret(secrets):
    _load_fixture_catalog()
    app = create_app()
    client = TestClient(app, follow_redirects=False)
    token = SENTINELS["ADDON_ACCESS_TOKEN"]
    paths = _get_paths(app) + ["/", "/configure", "/does-not-exist"]
    assert "/healthz" in paths
    assert f"/c/{token}/manifest.json" in paths
    assert client.get(f"/c/{token}/catalog/movie/trending_movie.json").json()["metas"][0]["id"] == "tt0000001"

    for path in paths:
        response = client.get(path, headers={"Origin": "https://example.org"})
        # The token is in the request path; it must not come back in the response.
        _assert_clean(response, secrets, path.replace(token, "<token>"))


def test_removed_upstream_routes_stay_gone():
    client = TestClient(create_app(), follow_redirects=False)
    assert client.get("/healthz").json() == {"status": "ok"}
    for path in ("/config/x", "/config/x/manifest.json", "/config/x/preview", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    assert client.post("/save-config", data={}).status_code in (404, 405)


def test_cors_only_on_the_stremio_sub_app():
    client = TestClient(create_app(), follow_redirects=False)
    origin = {"Origin": "https://web.stremio.com"}

    manifest = client.get("/c/public/manifest.json", headers=origin)
    assert manifest.status_code == 200
    assert manifest.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in manifest.headers

    preflight = client.options("/c/public/manifest.json", headers={**origin, "Access-Control-Request-Method": "GET"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"

    refused = client.options("/c/public/manifest.json", headers={**origin, "Access-Control-Request-Method": "POST"})
    assert refused.status_code == 400

    # Parent routes never send CORS headers.
    for response in (client.get("/healthz", headers=origin), client.get("/", headers=origin)):
        assert "access-control-allow-origin" not in response.headers


def test_request_log_masks_credential_path_segments():
    assert mask_url("http://h/c/TOKEN/catalog/movie/x.json") == "http://h/c/****/catalog/movie/x.json"
    assert mask_url("http://h/c/TOKEN/manifest.json?x=1") == "http://h/c/****/manifest.json"
    assert mask_url("http://h/healthz") == "http://h/healthz"


def test_routes_added_after_create_app_would_be_shadowed():
    """
    Guard for later milestones: the Stremio sub-app is a catch-all mounted last, so /ops and
    /configure must be registered inside create_app() before it, never added to the app afterwards.
    """
    app = create_app()
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
    client = TestClient(create_app())
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
    Logged in to /configure with every secret set (including the session secret and the password
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
    app = create_app()
    client = TestClient(app, follow_redirects=False)
    origin = {"Origin": "https://example.org"}
    responses = [client.get("/configure/login", headers=origin)]

    def csrf(html):
        return re.search(r'name="csrf" value="([^"]+)"', html).group(1)

    responses.append(
        client.post("/configure/login", data={"csrf": csrf(responses[0].text), "password": "wrong-one!!!!"})
    )
    responses.append(
        client.post("/configure/login", data={"csrf": csrf(responses[0].text), "password": SENTINELS["ADMIN_PASSWORD"]})
    )
    assert responses[-1].status_code == 303
    token = csrf(client.get("/configure/").text)
    paths = [p for p in _get_paths(app, "x") if p.startswith("/configure")]
    paths = [p.replace("trending_movie", "sweep_two") for p in paths] + ["/configure/catalogs/sweep_two/export.yaml"]
    assert "/configure/catalogs/sweep_two" in paths and "/configure/static/admin.js" in paths
    responses += [client.get(p, headers=origin) for p in paths]
    form = {"csrf": token, "mode": "form", "base_rev": rev}
    responses += [
        client.post("/configure/catalogs/sweep_two/save", data={**form, "prompt": "{{ nope }}", "action": "save"}),
        client.post(
            "/configure/catalogs/sweep_two/save",
            data={**form, "prompt": "Films {{ request_count }}", "action": "validate"},
        ),
        client.post("/configure/catalogs/sweep_two/dry-run", data={"csrf": token}),
        client.post("/configure/catalogs/sweep_two/rollback/1", data={"csrf": token}),
        client.post(
            "/configure/catalogs/sweep_two/save", data={**form, "prompt": "x", "action": "save"}, headers=origin
        ),
    ]
    assert [r.status_code for r in responses[-5:]] == [422, 200, 303, 409, 403]
    # The one deliberate exception: the signed-in catalog list shows the addon URL (with the
    # access token) so it can be copied into AIOMetadata. No other secret, and no other page.
    addon_token = SENTINELS["ADDON_ACCESS_TOKEN"]
    index = [r for r in responses if r.request.url.path == "/configure/" and r.status_code == 200]
    assert index and f"/c/{addon_token}/manifest.json" in index[0].text
    for r in responses:
        allowed = {addon_token} if r.request.url.path == "/configure/" and r.status_code == 200 else set()
        _assert_clean(r, [s for s in secrets if s not in allowed], r.request.url.path)
    for secret in secrets:
        assert secret not in caplog.text
