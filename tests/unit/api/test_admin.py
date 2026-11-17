"""
/admin, the browser editor (PLAN.md section 21): off by default, startup checks, login, cookie
flags, CSRF on every POST, the login rate limit, CORS isolation, editing through the definition
validation, file vs editor ownership, dry run and rollback.
"""

import json
import re
import time

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from app.api import admin as admin_mod
from app.api import create_app
from app.core.config import settings
from app.curation import editor
from app.curation.definitions import apply_dir
from app.store import db
from app.store.catalogs import SnapshotItem, accept_definition, publish_snapshot
from tests.unit.curation.pipeline_fakes import UniverseTMDB, make_universe

SECRET = "s" * 40
PASSWORD = "correct horse battery"
ORIGIN = {"Origin": "https://elsewhere.example"}
PROMPT = "Suggest {{ request_count }} films for category alpha.\n"


@pytest.fixture
def tmdb():
    return UniverseTMDB(make_universe(20), {"collection:Saga Nine": [{"id": 900, "name": "Saga Nine Collection"}]})


@pytest.fixture
def editor_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ENABLE_EDITOR", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", SECRET)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    catalogs_dir = tmp_path / "catalogs.d"
    catalogs_dir.mkdir()
    monkeypatch.setattr(settings, "CATALOGS_DIR", str(catalogs_dir))
    return catalogs_dir


@pytest.fixture
def app(editor_on, tmdb):
    application = create_app(enable_legacy=False)
    application.state.admin_app.state.tmdb_client_factory = tmdb.client
    return application


@pytest.fixture
def anon(app):
    return TestClient(app, follow_redirects=False)


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no CSRF field on the page"
    return m.group(1)


def login(client: TestClient, password: str = PASSWORD):
    page = client.get("/admin/login")
    return client.post("/admin/login", data={"csrf": csrf_of(page.text), "password": password})


@pytest.fixture
def client(anon):
    r = login(anon)
    assert r.status_code == 303, r.text
    return anon


def token(client) -> str:
    return csrf_of(client.get("/admin/").text)


def catalog_row(cid):
    with db.session() as conn:
        r = conn.execute("SELECT * FROM catalog_defs WHERE catalog_id = ?", (cid,)).fetchone()
        return dict(r) if r else None


def add_catalog(cid="alpha_list", source="editor", **extra):
    definition = {"prompt": PROMPT, **extra}
    with db.session() as conn, db.transaction(conn):
        state = "disabled" if definition.get("enabled") is False else "enabled"
        rev = accept_definition(conn, cid, "Alpha", definition, state=state, position=definition.get("position") or 0)
        conn.execute("UPDATE catalog_defs SET source = ? WHERE catalog_id = ?", (source, cid))
    return rev


def post_form(client, path, definition_values, **extra):
    data = {"csrf": token(client), "mode": "form", **definition_values, **extra}
    return client.post(path, data=data)


# --------------------------------------------------------------------------- off / startup


def test_disabled_by_default_every_path_404(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_EDITOR", False)
    c = TestClient(create_app(enable_legacy=False), follow_redirects=False)
    for method, path in [("GET", "/admin/"), ("GET", "/admin/login"), ("POST", "/admin/login"), ("GET", "/admin/x")]:
        r = c.request(method, path, headers=ORIGIN)
        assert r.status_code == 404
        assert not any(h.startswith("access-control-") for h in r.headers)


@pytest.mark.parametrize(
    "secret, password",
    [(None, PASSWORD), ("short", PASSWORD), ("é" * 15, PASSWORD), (SECRET, None), (SECRET, "elevenchars")],
)
def test_startup_fails_without_strong_secrets(monkeypatch, secret, password):
    monkeypatch.setattr(settings, "ENABLE_EDITOR", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", secret)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", password)
    with pytest.raises(admin_mod.EditorConfigError):
        create_app(enable_legacy=False)


def test_secret_length_is_counted_in_bytes(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_EDITOR", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "é" * 16)  # 16 characters, 32 bytes
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", PASSWORD)
    create_app(enable_legacy=False)


# --------------------------------------------------------------------------- auth


def test_unauthenticated_requests_learn_nothing(anon):
    add_catalog()
    for path in ["/admin/", "/admin/catalogs/alpha_list", "/admin/nope", "/admin/static/admin.js"]:
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert anon.get("/admin/static/admin.css").headers["content-type"].startswith("text/css")  # login page style
    for path in ["/admin/catalogs/alpha_list/dry-run", "/admin/new", "/admin/nope"]:
        assert anon.post(path, data={"csrf": "x"}).status_code == 401
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_login_sets_strict_cookie(anon):
    r = login(anon)
    assert r.status_code == 303 and r.headers["location"] == "/admin/"
    cookie = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("aic_admin=")][0]
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/admin" in lowered
    assert "max-age=43200" in lowered
    assert "secure" not in lowered
    assert anon.get("/admin/").status_code == 200


def test_cookie_is_secure_when_public_url_is_https(app, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://catalogs.example")
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    r = login(c)
    cookie = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("aic_admin=")][0]
    assert "secure" in cookie.lower()


def test_wrong_password_is_401_and_sets_no_session(anon):
    r = login(anon, "wrong password!")
    assert r.status_code == 401 and "Wrong password" in r.text
    assert "aic_admin" not in anon.cookies
    assert anon.get("/admin/").status_code == 303


def test_login_needs_csrf_and_the_prelogin_cookie(anon):
    page = anon.get("/admin/login")
    assert anon.post("/admin/login", data={"password": PASSWORD}).status_code == 403
    assert anon.post("/admin/login", data={"csrf": "forged", "password": PASSWORD}).status_code == 403
    fresh = TestClient(anon.app, follow_redirects=False)  # no pre-login cookie
    assert fresh.post("/admin/login", data={"csrf": csrf_of(page.text), "password": PASSWORD}).status_code == 403
    assert "aic_admin" not in anon.cookies


def _replace_session(client, value):
    client.cookies.clear()
    client.cookies.set("aic_admin", value)


def test_tampered_or_expired_session_is_rejected(client):
    value = client.cookies.get("aic_admin")
    body, mac = value.split(".")
    payload = json.loads(admin_mod._unb64(body))
    payload["exp"] += 3600
    _replace_session(client, value)
    assert client.get("/admin/").status_code == 200  # the jar swap itself keeps the session
    forged = admin_mod._b64(json.dumps(payload).encode()) + "." + mac
    _replace_session(client, forged)
    assert client.get("/admin/").status_code == 303
    expired = admin_mod.sign("session", {"sid": "x", "iat": 0, "exp": int(time.time()) - 1})
    _replace_session(client, expired)
    assert client.get("/admin/").status_code == 303
    prelogin = admin_mod.sign("prelogin", {"sid": "x", "exp": int(time.time()) + 60})  # wrong purpose
    _replace_session(client, prelogin)
    assert client.get("/admin/").status_code == 303


def test_logout_clears_the_cookie(client):
    r = client.post("/admin/logout", data={"csrf": token(client)})
    assert r.status_code == 303
    assert client.get("/admin/").status_code == 303


def test_login_rate_limit_per_client(anon):
    for _ in range(admin_mod.LOGIN_MAX_PER_CLIENT):
        assert login(anon, "wrong password!").status_code == 401
    r = login(anon)  # even the right password is refused while blocked
    assert r.status_code == 429 and "aic_admin" not in anon.cookies


def test_login_rate_limit_overall(anon, monkeypatch):
    with db.session() as conn, db.transaction(conn):
        for i in range(admin_mod.LOGIN_MAX_TOTAL):
            conn.execute("INSERT INTO admin_login_failures (client, at) VALUES (?, ?)", (f"10.0.0.{i}", db.now_ms()))
    assert login(anon).status_code == 429
    with db.session() as conn:  # old failures expire
        conn.execute("UPDATE admin_login_failures SET at = at - ?", (admin_mod.LOGIN_WINDOW_MS + 1,))
    assert login(anon).status_code == 303


# --------------------------------------------------------------------------- CSRF


def _post_paths(app) -> list[str]:
    admin_app = app.state.admin_app
    fill = {"catalog_id": "alpha_list", "snapshot_id": "1"}
    out = []
    for route in admin_app.routes:
        if isinstance(route, Route) and "POST" in (route.methods or ()) and route.path != "/login":
            path = route.path
            for k, v in fill.items():
                path = path.replace("{" + k + "}", v)
            out.append("/admin" + path)
    return out


def test_every_post_route_requires_csrf_and_same_origin(client, app):
    rev = add_catalog()
    paths = _post_paths(app)
    assert {"/admin/logout", "/admin/new", "/admin/catalogs/alpha_list/save"} <= set(paths)
    good = token(client)
    form = {"base_rev": rev, "state": "disabled", "mode": "form", "prompt": PROMPT, "action": "save"}
    for path in paths:
        assert client.post(path, data=form).status_code == 403, path
        assert client.post(path, data={**form, "csrf": "nope"}).status_code == 403, path
        r = client.post(path, data={**form, "csrf": good}, headers=ORIGIN)
        assert r.status_code == 403, path
        r = client.post(path, data={**form, "csrf": good}, headers={"Origin": "null"})
        assert r.status_code == 403, path
    assert catalog_row("alpha_list")["config_rev"] == rev and catalog_row("alpha_list")["state"] == "enabled"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert client.get("/admin/").status_code == 200  # still logged in: logout was refused too


def test_csrf_token_is_bound_to_the_session(client, anon, app):
    add_catalog()
    other = TestClient(app, follow_redirects=False)
    login(other)
    r = client.post("/admin/catalogs/alpha_list/dry-run", data={"csrf": token(other)})
    assert r.status_code == 403


def test_same_origin_post_is_accepted(client):
    add_catalog()
    r = client.post(
        "/admin/catalogs/alpha_list/dry-run", data={"csrf": token(client)}, headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 303


# --------------------------------------------------------------------------- CORS and headers


@pytest.mark.parametrize("authorised", [True, False])
def test_admin_is_outside_stremio_cors(app, authorised):
    c = TestClient(app, follow_redirects=False)
    if authorised:
        login(c)
    add_catalog()
    requests = [
        ("GET", "/admin/"),
        ("GET", "/admin/catalogs/alpha_list/status.json"),
        ("GET", "/admin/missing/path"),
        ("POST", "/admin/catalogs/alpha_list/dry-run"),
        ("OPTIONS", "/admin/"),
        ("OPTIONS", "/admin/missing/path"),
    ]
    for method, path in requests:
        headers = {**ORIGIN, "Access-Control-Request-Method": "POST"}
        r = c.request(method, path, headers=headers)
        assert not any(h.lower().startswith("access-control-") for h in r.headers), (method, path)
        assert r.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"


def test_missing_path_is_404_after_login(client):
    assert client.get("/admin/missing/path").status_code == 404
    assert client.get("/admin/catalogs/none_here").status_code == 404
    assert client.get("/admin/static/other.js").status_code == 404
    assert client.get("/admin/static/admin.js").headers["content-type"].startswith("text/javascript")


def test_pages_have_no_inline_script_or_style(client):
    add_catalog()
    for path in ["/admin/", "/admin/new", "/admin/catalogs/alpha_list", "/admin/login"]:
        html = client.get(path).text
        assert "<script>" not in html and "style=" not in html and "<style" not in html
        assert "onclick" not in html


# --------------------------------------------------------------------------- editing


def values_for(definition):
    return editor.form_values(definition)


def test_create_catalog_from_the_form(client):
    vals = values_for({"prompt": PROMPT, "categories": ["alpha", "beta"], "min_vote_average": 6.5})
    r = post_form(client, "/admin/new", vals, catalog_id="new_one", action="save")
    assert r.status_code == 303 and r.headers["location"] == "/admin/catalogs/new_one?msg=created"
    row = catalog_row("new_one")
    assert row["source"] == "editor" and row["state"] == "enabled"
    assert json.loads(row["definition_json"]) == {
        "prompt": PROMPT,
        "categories": ["alpha", "beta"],
        "min_vote_average": 6.5,
    }
    page = client.get(r.headers["location"]).text
    assert "Catalog created." in page and "New One" in page


def test_validation_errors_are_shown_and_nothing_is_stored(client):
    cases = [
        ({"prompt": PROMPT, "min_vote_average": "11"}, "min_vote_average"),
        ({"prompt": "Suggest {{ nonsense }} films"}, "unknown variable"),
        ({"prompt": PROMPT, "year_min": "2000", "year_max": "1990"}, "year_min must not be greater"),
        ({"prompt": PROMPT, "target_count": "abc"}, "target_count: not a number"),
        ({"prompt": PROMPT, "schedule": "cron: '0 6 * *'"}, "schedule"),
        ({"prompt": PROMPT, "decade_min": "[unclosed"}, "decade_min: invalid YAML"),
        ({"prompt": PROMPT, "on_shortfall": "publish_partial"}, "min_accept"),
        ({"prompt": ""}, "prompt"),
    ]
    for values, expected in cases:
        for action in ("validate", "save"):
            r = post_form(client, "/admin/new", values, catalog_id="bad_one", action=action)
            assert r.status_code == 422, (values, r.text[:300])
            assert "Not saved" in r.text and expected in r.text, (values, expected)
    assert catalog_row("bad_one") is None
    r = post_form(client, "/admin/new", {"prompt": PROMPT}, catalog_id="Bad-Id", action="save")
    assert r.status_code == 422 and "catalog id" in r.text


def test_submitted_values_are_kept_and_escaped_after_an_error(client):
    r = post_form(client, "/admin/new", {"prompt": "<b>{{ nonsense }}</b>", "name": "<i>x</i>"}, catalog_id="c1")
    assert r.status_code == 422
    assert "<b>{{" not in r.text and "&lt;b&gt;{{ nonsense }}&lt;/b&gt;" in r.text
    assert 'value="&lt;i&gt;x&lt;/i&gt;"' in r.text


def test_validate_shows_the_prompt_preview_and_saves_nothing(client):
    rev = add_catalog()
    vals = values_for({"prompt": PROMPT, "year_min": 1970})
    r = post_form(client, "/admin/catalogs/alpha_list/save", vals, base_rev=rev, action="validate")
    assert r.status_code == 200 and "Valid. Not saved yet." in r.text
    assert "films for category alpha" in r.text and "1970" in r.text  # rendered prompt + constraints block
    assert catalog_row("alpha_list")["config_rev"] == rev


def test_raw_yaml_tab_saves_and_reports_yaml_errors(client):
    rev = add_catalog()
    data = {"csrf": token(client), "mode": "yaml", "base_rev": rev, "action": "save"}
    r = client.post("/admin/catalogs/alpha_list/save", data={**data, "yaml": "prompt: [unclosed"})
    assert r.status_code == 422 and "invalid YAML" in r.text
    r = client.post(
        "/admin/catalogs/alpha_list/save", data={**data, "yaml": f"prompt: |\n  {PROMPT}\r\ntarget_count: 50\r\n"}
    )
    assert r.status_code == 303
    assert json.loads(catalog_row("alpha_list")["definition_json"]) == {"prompt": PROMPT, "target_count": 50}


def test_save_bumps_revision_and_epoch(client):
    rev = add_catalog()
    before = catalog_row("alpha_list")
    vals = values_for({"prompt": PROMPT, "min_runtime": 80})
    r = post_form(client, "/admin/catalogs/alpha_list/save", vals, base_rev=rev, action="save")
    assert r.status_code == 303
    after = catalog_row("alpha_list")
    assert after["config_rev"] != rev and after["epoch"] == before["epoch"] + 1


ROUND_TRIP_DEFINITIONS = [
    {"prompt": PROMPT},
    {
        "prompt": "Line one\nLine two  \n\n{{ request_count }}\n",
        "name": "Alpha List",
        "enabled": True,
        "categories": [],
        "anchors": ["Film 1", "Film 2 (1999)"],
        "weekday_focus": {"friday": "something light"},
        "min_vote_average": 7,
        "min_vote_count": 300,
        "overgenerate_factor": 2,
        "target_count": 100,
        "temperature": 0.7,
        "timeout_s": 120.0,
        "languages": ["fr", "it"],
        "excluded_genre_ids": [99],
        "decade_min": {"1970": 15},
        "blacklist_titles": ["Film 3", "Film 4 (2001)"],
        "franchise_denylist": {"collections": ["Saga Nine"], "companies": [], "keywords": [], "title_patterns": []},
        "exclude_watched": False,
        "history_mode": "strict",
        "schedule": {"cron": "0 6 * * *", "timezone": "Europe/Berlin"},
        "focus": None,
    },
    {"prompt": PROMPT, "enabled": False, "on_shortfall": "publish_partial", "min_accept": 80, "position": 3},
]


@pytest.mark.parametrize("definition", ROUND_TRIP_DEFINITIONS)
def test_untouched_form_keeps_the_revision(client, definition):
    rev = add_catalog(**{k: v for k, v in definition.items() if k != "prompt"}, prompt=definition["prompt"])
    epoch = catalog_row("alpha_list")["epoch"]
    vals = values_for(definition)
    # Browsers submit textareas with CRLF line endings; unchecked boxes are not submitted.
    submitted = {k: v.replace("\n", "\r\n") for k, v in vals.items() if v != "" or k == "prompt"}
    r = post_form(client, "/admin/catalogs/alpha_list/save", submitted, base_rev=rev, action="save")
    assert r.status_code == 303, r.text[:500]
    row = catalog_row("alpha_list")
    assert json.loads(row["definition_json"]) == definition
    assert row["config_rev"] == rev and row["epoch"] == epoch


def test_rendered_form_round_trips_from_the_html(client):
    """The values the page itself renders (not editor.form_values) submit back unchanged."""
    definition = ROUND_TRIP_DEFINITIONS[1]
    rev = add_catalog(**definition)
    html = client.get("/admin/catalogs/alpha_list").text
    form_html = html.split('id="form-tab"', 1)[1].split("</form>", 1)[0]
    data = {}
    for m in re.finditer(r'<input id="f-(\w+)" name="\1"[^>]*?value="([^"]*)"', form_html):
        data[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input id="f-(\w+)" type="checkbox" name="\1" value="on" checked', form_html):
        data[m.group(1)] = "on"
    for m in re.finditer(r'<textarea id="f-(\w+)" name="\1"[^>]*>\n(.*?)</textarea>', form_html, re.S):
        data[m.group(1)] = m.group(2)
    for m in re.finditer(r'<select id="f-(\w+)" name="\1">(.*?)</select>', form_html, re.S):
        sel = re.search(r'<option value="([^"]*)" selected', m.group(2))
        data[m.group(1)] = sel.group(1) if sel else ""
    import html as html_lib

    data = {k: html_lib.unescape(v) for k, v in data.items()}
    r = post_form(client, "/admin/catalogs/alpha_list/save", data, base_rev=rev, action="save")
    assert r.status_code == 303, r.text[:800]
    assert catalog_row("alpha_list")["config_rev"] == rev


def test_stale_form_is_refused(client):
    rev = add_catalog()
    vals = values_for({"prompt": PROMPT, "min_runtime": 80})
    assert post_form(client, "/admin/catalogs/alpha_list/save", vals, base_rev=rev, action="save").status_code == 303
    r = post_form(
        client, "/admin/catalogs/alpha_list/save", values_for({"prompt": "Other"}), base_rev=rev, action="save"
    )
    assert r.status_code == 409 and "changed elsewhere" in r.text
    assert json.loads(catalog_row("alpha_list")["definition_json"])["min_runtime"] == 80


def test_new_catalog_cannot_overwrite_an_existing_one(client):
    rev = add_catalog()
    r = post_form(client, "/admin/new", values_for({"prompt": "Other"}), catalog_id="alpha_list", action="save")
    assert r.status_code == 422 and "already exists" in r.text
    assert catalog_row("alpha_list")["config_rev"] == rev


def test_exclusion_names_are_resolved_on_save(client, tmdb):
    vals = values_for({"prompt": PROMPT, "franchise_denylist": {"collections": ["Saga Nine", "Saga Unknown"]}})
    r = post_form(client, "/admin/new", vals, catalog_id="excl", action="validate")
    assert r.status_code == 200 and "No TMDB match for" in r.text and "Saga Unknown" in r.text
    r = post_form(client, "/admin/new", vals, catalog_id="excl", action="save")
    assert r.status_code == 303
    resolved = json.loads(catalog_row("excl")["exclusions_json"])
    assert resolved["collection_ids"] == [900]


def test_tmdb_failure_saves_nothing(client, tmdb):
    tmdb.fail_with = 503
    vals = values_for({"prompt": PROMPT, "blacklist_titles": ["Film 3"]})
    r = post_form(client, "/admin/new", vals, catalog_id="excl", action="save")
    assert r.status_code == 422 and "could not be resolved on TMDB" in r.text
    assert catalog_row("excl") is None


def test_disable_enable_and_delete(client):
    rev = add_catalog()
    r = client.post(
        "/admin/catalogs/alpha_list/state", data={"csrf": token(client), "state": "disabled", "base_rev": rev}
    )
    assert r.status_code == 303
    row = catalog_row("alpha_list")
    assert row["state"] == "disabled" and json.loads(row["definition_json"])["enabled"] is False
    r = client.post(
        "/admin/catalogs/alpha_list/state",
        data={"csrf": token(client), "state": "enabled", "base_rev": row["config_rev"]},
    )
    row = catalog_row("alpha_list")
    assert row["state"] == "enabled" and json.loads(row["definition_json"]) == {"prompt": PROMPT}
    r = client.post(
        "/admin/catalogs/alpha_list/state",
        data={"csrf": token(client), "state": "removed", "base_rev": row["config_rev"]},
    )
    assert r.status_code == 303 and catalog_row("alpha_list")["state"] == "removed"
    assert client.get("/admin/catalogs/alpha_list").status_code == 404


def test_export_yaml_round_trips(client):
    definition = ROUND_TRIP_DEFINITIONS[1]
    add_catalog(**definition)
    r = client.get("/admin/catalogs/alpha_list/export.yaml")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert editor.parse_definition_yaml(r.text) == definition


# --------------------------------------------------------------------------- file vs editor


async def test_file_catalog_is_read_only_until_moved(client, editor_on, tmdb):
    (editor_on / "from_file.yaml").write_text(f"prompt: |\n  {PROMPT}min_runtime: 70\n", encoding="utf-8")
    await apply_dir(editor_on, tmdb_client_factory=tmdb.client)
    row = catalog_row("from_file")
    assert row["source"] == "file"
    page = client.get("/admin/catalogs/from_file").text
    assert "Move to editor" in page and "<fieldset disabled>" in page.replace("fieldset disabled", "fieldset disabled")
    vals = values_for({"prompt": "Changed"})
    r = post_form(client, "/admin/catalogs/from_file/save", vals, base_rev=row["config_rev"], action="save")
    assert r.status_code == 409 and catalog_row("from_file")["config_rev"] == row["config_rev"]

    r = client.post("/admin/catalogs/from_file/move-to-editor", data={"csrf": token(client)})
    assert r.status_code == 303
    moved = catalog_row("from_file")
    assert moved["source"] == "editor"
    assert (moved["config_rev"], moved["epoch"]) == (row["config_rev"], row["epoch"])  # no epoch bump
    page = client.get("/admin/catalogs/from_file").text
    assert "still exists and is ignored" in page

    # The file is inert now: an edited file is ignored, and so is its removal.
    (editor_on / "from_file.yaml").write_text("prompt: [broken", encoding="utf-8")
    report = await apply_dir(editor_on, tmdb_client_factory=tmdb.client)
    assert [r.status for r in report.files] == ["ignored"] and report.ok
    assert catalog_row("from_file")["config_rev"] == row["config_rev"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM definition_errors").fetchone()[0] == 0
    (editor_on / "from_file.yaml").unlink()
    report = await apply_dir(editor_on, tmdb_client_factory=tmdb.client)
    assert report.removed == [] and catalog_row("from_file")["state"] == "enabled"

    vals = values_for({"prompt": PROMPT, "min_runtime": 90})
    r = post_form(client, "/admin/catalogs/from_file/save", vals, base_rev=row["config_rev"], action="save")
    assert r.status_code == 303 and json.loads(catalog_row("from_file")["definition_json"])["min_runtime"] == 90


# --------------------------------------------------------------------------- dry run and rollback


def test_dry_run_is_queued_and_its_result_shown(client):
    rev = add_catalog(max_runs_per_day=1)
    r = client.post("/admin/catalogs/alpha_list/dry-run", data={"csrf": token(client)})
    assert r.status_code == 303 and r.headers["location"].endswith("msg=dry_run_queued")
    with db.session() as conn:
        job = conn.execute("SELECT id, kind, state, trigger FROM jobs").fetchone()
    assert (job["kind"], job["state"], job["trigger"]) == ("dry_run", "pending", "admin")
    page = client.get(r.headers["location"]).text
    assert "Dry run queued" in page and f"Job {job['id']} (dry_run) is pending" in page
    assert client.get("/admin/catalogs/alpha_list/status.json").json()["open_job"]["id"] == job["id"]
    r = client.post("/admin/catalogs/alpha_list/dry-run", data={"csrf": token(client)})
    assert r.headers["location"].endswith("msg=already_open")

    with db.session() as conn, db.transaction(conn):  # the worker ran it
        conn.execute("UPDATE jobs SET state = 'done'")
        conn.execute(
            "INSERT INTO runs (job_id, catalog_id, config_rev, epoch, trigger, dry_run, started_at, status, "
            "stage_counts_json, reject_reasons_json, model_calls) VALUES (?, 'alpha_list', ?, 0, 'admin', 1, ?, "
            "'ok', ?, ?, 3)",
            (
                job["id"],
                rev,
                db.now_ms(),
                json.dumps({"suggested": 160, "eligible": 104}),
                json.dumps({"min_vote_average": 21, "<script>": 1}),
            ),
        )
    assert client.get("/admin/catalogs/alpha_list/status.json").json()["open_job"] is None
    page = client.get("/admin/catalogs/alpha_list").text
    assert "suggested: 160" in page and "min_vote_average: 21" in page
    assert "&lt;script&gt;: 1" in page and "<script>: 1" not in page


def test_dry_run_of_a_disabled_catalog_is_refused(client):
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, "alpha_list", "Alpha", {"prompt": PROMPT}, state="disabled")
    r = client.post("/admin/catalogs/alpha_list/dry-run", data={"csrf": token(client)})
    assert r.headers["location"].endswith("msg=not_enabled")
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def _publish(rev, n):
    with db.session() as conn, db.transaction(conn):
        return publish_snapshot(conn, "alpha_list", [SnapshotItem(f"tt{n:07d}", f"Film {n}", 2000, n)], config_rev=rev)


def _active_id():
    with db.session() as conn:
        return conn.execute("SELECT id FROM snapshots WHERE status = 'active'").fetchone()["id"]


def test_rollback_and_refusal(client):
    rev = add_catalog(source="file")  # rollback works for file catalogs too
    _publish(rev, 1)
    _publish(rev, 2)
    _publish(rev, 3)
    with db.session() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM snapshots ORDER BY id")]
    page = client.get("/admin/catalogs/alpha_list").text
    assert f"/rollback/{ids[0]}" in page and f"/rollback/{ids[2]}" not in page  # active one has no button
    confirm = client.get(f"/admin/catalogs/alpha_list/rollback/{ids[0]}")
    assert confirm.status_code == 200 and f"Roll back to list {ids[0]}?" in confirm.text
    epoch = catalog_row("alpha_list")["epoch"]
    r = client.post(f"/admin/catalogs/alpha_list/rollback/{ids[0]}", data={"csrf": token(client)})
    assert r.status_code == 303 and r.headers["location"].endswith("msg=rolled_back")
    assert _active_id() == ids[0] and catalog_row("alpha_list")["epoch"] == epoch + 1
    page = client.get(r.headers["location"]).text
    assert "Rolled back" in page and f"/rollback/{ids[1]}" not in page  # revoked now

    r = client.post(f"/admin/catalogs/alpha_list/rollback/{ids[1]}", data={"csrf": token(client)})
    assert r.status_code == 409 and "revoked" in r.text
    assert _active_id() == ids[0]


# --------------------------------------------------------------------------- generated form


def test_every_definition_field_gets_a_widget():
    from app.curation.definitions import CatalogDefinition

    specs = {s.name: s for s in editor.editor_fields()}
    assert set(specs) == set(CatalogDefinition.model_fields)
    assert specs["min_vote_average"].slider and (
        specs["min_vote_average"].minimum,
        specs["min_vote_average"].maximum,
    ) == (0, 10)
    assert specs["year_min"].slider and specs["temperature"].slider
    assert specs["min_vote_count"].kind == "number" and not specs["min_vote_count"].slider
    assert specs["history_mode"].kind == "select" and specs["history_mode"].choices == ["strict", "relax"]
    assert specs["exclude_watched"].kind == "bool"
    assert specs["languages"].kind == "lines" and specs["excluded_genre_ids"].number_type is int
    assert specs["schedule"].kind == "yaml" and specs["prompt"].kind == "textarea"


def test_a_new_definition_field_appears_without_editor_changes(monkeypatch):
    from pydantic import create_model

    from app.curation import definitions

    extended = create_model(
        "Extended",
        __base__=definitions.CatalogDefinition,
        future_floor=(float | None, definitions.Field(default=None, ge=0, le=5)),
    )
    monkeypatch.setattr(editor, "CatalogDefinition", extended)
    spec = {s.name: s for s in editor.editor_fields()}["future_floor"]
    assert spec.group == editor.OTHER_GROUP and spec.slider and spec.maximum == 5
