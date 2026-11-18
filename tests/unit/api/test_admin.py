"""
/configure, the browser editor (PLAN.md section 21): off by default, startup checks, login, cookie
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
    application = create_app()
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
    page = client.get("/configure/login")
    return client.post("/configure/login", data={"csrf": csrf_of(page.text), "password": password})


@pytest.fixture
def client(anon):
    r = login(anon)
    assert r.status_code == 303, r.text
    return anon


def token(client) -> str:
    return csrf_of(client.get("/configure/").text)


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
    c = TestClient(create_app(), follow_redirects=False)
    paths = [
        ("GET", "/configure"),
        ("GET", "/configure/"),
        ("GET", "/configure/login"),
        ("POST", "/configure/login"),
        ("GET", "/configure/x"),
    ]
    for method, path in paths:
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
        create_app()


def test_secret_length_is_counted_in_bytes(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_EDITOR", True)
    monkeypatch.setattr(settings, "SESSION_SECRET", "é" * 16)  # 16 characters, 32 bytes
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", PASSWORD)
    create_app()


# --------------------------------------------------------------------------- auth


def test_unauthenticated_requests_learn_nothing(anon):
    add_catalog()
    for path in ["/configure/", "/configure/catalogs/alpha_list", "/configure/nope", "/configure/static/admin.js"]:
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/configure/login"
    assert anon.get("/configure/static/admin.css").headers["content-type"].startswith("text/css")  # login page style
    for path in ["/configure/catalogs/alpha_list/dry-run", "/configure/new", "/configure/nope"]:
        assert anon.post(path, data={"csrf": "x"}).status_code == 401
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_login_sets_strict_cookie(anon):
    r = login(anon)
    assert r.status_code == 303 and r.headers["location"] == "/configure/"
    cookie = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("aic_admin=")][0]
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/configure" in lowered
    assert "max-age=43200" in lowered
    assert "secure" not in lowered
    assert anon.get("/configure/").status_code == 200


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
    assert anon.get("/configure/").status_code == 303


def test_login_needs_csrf_and_the_prelogin_cookie(anon):
    page = anon.get("/configure/login")
    assert anon.post("/configure/login", data={"password": PASSWORD}).status_code == 403
    assert anon.post("/configure/login", data={"csrf": "forged", "password": PASSWORD}).status_code == 403
    fresh = TestClient(anon.app, follow_redirects=False)  # no pre-login cookie
    assert fresh.post("/configure/login", data={"csrf": csrf_of(page.text), "password": PASSWORD}).status_code == 403
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
    assert client.get("/configure/").status_code == 200  # the jar swap itself keeps the session
    forged = admin_mod._b64(json.dumps(payload).encode()) + "." + mac
    _replace_session(client, forged)
    assert client.get("/configure/").status_code == 303
    expired = admin_mod.sign("session", {"sid": "x", "iat": 0, "exp": int(time.time()) - 1})
    _replace_session(client, expired)
    assert client.get("/configure/").status_code == 303
    prelogin = admin_mod.sign("prelogin", {"sid": "x", "exp": int(time.time()) + 60})  # wrong purpose
    _replace_session(client, prelogin)
    assert client.get("/configure/").status_code == 303


def test_logout_clears_the_cookie(client):
    r = client.post("/configure/logout", data={"csrf": token(client)})
    assert r.status_code == 303
    assert client.get("/configure/").status_code == 303


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
            out.append("/configure" + path)
    return out


def test_every_post_route_requires_csrf_and_same_origin(client, app):
    rev = add_catalog()
    paths = _post_paths(app)
    assert {"/configure/logout", "/configure/new", "/configure/catalogs/alpha_list/save"} <= set(paths)
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
    assert client.get("/configure/").status_code == 200  # still logged in: logout was refused too


def test_csrf_token_is_bound_to_the_session(client, anon, app):
    add_catalog()
    other = TestClient(app, follow_redirects=False)
    login(other)
    r = client.post("/configure/catalogs/alpha_list/dry-run", data={"csrf": token(other)})
    assert r.status_code == 403


def test_same_origin_post_is_accepted(client):
    add_catalog()
    r = client.post(
        "/configure/catalogs/alpha_list/dry-run", data={"csrf": token(client)}, headers={"Origin": "http://testserver"}
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
        ("GET", "/configure"),
        ("OPTIONS", "/configure"),
        ("GET", "/configure/"),
        ("GET", "/configure/catalogs/alpha_list/status.json"),
        ("GET", "/configure/missing/path"),
        ("POST", "/configure/catalogs/alpha_list/dry-run"),
        ("OPTIONS", "/configure/"),
        ("OPTIONS", "/configure/missing/path"),
    ]
    for method, path in requests:
        headers = {**ORIGIN, "Access-Control-Request-Method": "POST"}
        r = c.request(method, path, headers=headers)
        assert not any(h.lower().startswith("access-control-") for h in r.headers), (method, path)
        if path == "/configure":
            assert r.status_code == 308 and r.headers["location"] == "/configure/"
            continue
        assert r.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"


def test_missing_path_is_404_after_login(client):
    assert client.get("/configure/missing/path").status_code == 404
    assert client.get("/configure/catalogs/none_here").status_code == 404
    assert client.get("/configure/static/other.js").status_code == 404
    assert client.get("/configure/static/admin.js").headers["content-type"].startswith("text/javascript")


def test_pages_have_no_inline_script_or_style(client):
    add_catalog()
    for path in ["/configure/", "/configure/new", "/configure/catalogs/alpha_list", "/configure/login"]:
        html = client.get(path).text
        assert "<script>" not in html and "style=" not in html and "<style" not in html
        assert "onclick" not in html


# --------------------------------------------------------------------------- editing


def values_for(definition):
    return editor.form_values(definition)


def test_create_catalog_from_the_form(client):
    vals = values_for({"prompt": PROMPT, "categories": ["alpha", "beta"], "min_vote_average": 6.5})
    r = post_form(client, "/configure/new", vals, catalog_id="new_one", action="save")
    assert r.status_code == 303 and r.headers["location"] == "/configure/catalogs/new_one?msg=created"
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
        ({"prompt": PROMPT, "min_vote_average": "11"}, "Minimum rating: must be at most 10"),
        ({"prompt": "Suggest {{ nonsense }} films"}, "Remove the curly braces"),
        (
            {"prompt": PROMPT, "year_min": "2000", "year_max": "1990"},
            "“Released from” must not be greater than “Released until”",
        ),
        ({"prompt": PROMPT, "target_count": "abc"}, "Movies in the catalog: enter a number"),
        ({"prompt": PROMPT, "schedule": "cron: '0 6 * *'"}, "Refresh: a custom schedule needs five parts"),
        ({"prompt": PROMPT, "decade_min": "[unclosed"}, "Minimum movies per decade: couldn&#39;t read this value"),
        ({"prompt": PROMPT, "on_shortfall": "publish_partial"}, "Smallest catalog worth publishing"),
        ({"prompt": ""}, "What kind of movies do you want?"),
    ]
    for values, expected in cases:
        for action in ("validate", "save"):
            r = post_form(client, "/configure/new", values, catalog_id="bad_one", action=action)
            assert r.status_code == 422, (values, r.text[:300])
            assert "Not saved" in r.text and expected in r.text, (values, expected)
    assert catalog_row("bad_one") is None
    r = post_form(client, "/configure/new", {"prompt": PROMPT}, catalog_id="Bad-Id", action="save")
    assert r.status_code == 422 and "catalog id" in r.text


def test_submitted_values_are_kept_and_escaped_after_an_error(client):
    r = post_form(client, "/configure/new", {"prompt": "<b>{{ nonsense }}</b>", "name": "<i>x</i>"}, catalog_id="c1")
    assert r.status_code == 422
    assert "<b>{{" not in r.text and "&lt;b&gt;{{ nonsense }}&lt;/b&gt;" in r.text
    assert 'value="&lt;i&gt;x&lt;/i&gt;"' in r.text


def test_validate_shows_the_prompt_preview_and_saves_nothing(client):
    rev = add_catalog()
    vals = values_for({"prompt": PROMPT, "year_min": 1970})
    r = post_form(client, "/configure/catalogs/alpha_list/save", vals, base_rev=rev, action="validate")
    assert r.status_code == 200 and "Looks good. Not saved yet." in r.text
    assert "films for category alpha" in r.text and "1970" in r.text  # rendered prompt + constraints block
    assert catalog_row("alpha_list")["config_rev"] == rev


def test_raw_yaml_tab_saves_and_reports_yaml_errors(client):
    rev = add_catalog()
    data = {"csrf": token(client), "mode": "yaml", "base_rev": rev, "action": "save"}
    r = client.post("/configure/catalogs/alpha_list/save", data={**data, "yaml": "prompt: [unclosed"})
    assert r.status_code == 422 and "invalid YAML" in r.text
    r = client.post(
        "/configure/catalogs/alpha_list/save", data={**data, "yaml": f"prompt: |\n  {PROMPT}\r\ntarget_count: 50\r\n"}
    )
    assert r.status_code == 303
    assert json.loads(catalog_row("alpha_list")["definition_json"]) == {"prompt": PROMPT, "target_count": 50}


def test_save_bumps_revision_and_epoch(client):
    rev = add_catalog()
    before = catalog_row("alpha_list")
    vals = values_for({"prompt": PROMPT, "min_runtime": 80})
    r = post_form(client, "/configure/catalogs/alpha_list/save", vals, base_rev=rev, action="save")
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
    {"prompt": PROMPT, "languages": "fr", "excluded_genre_ids": [], "min_vote_average": 6.25},
]


@pytest.mark.parametrize("definition", ROUND_TRIP_DEFINITIONS)
def test_untouched_form_keeps_the_revision(client, definition):
    rev = add_catalog(**{k: v for k, v in definition.items() if k != "prompt"}, prompt=definition["prompt"])
    epoch = catalog_row("alpha_list")["epoch"]
    vals = values_for(definition)
    # Browsers submit textareas with CRLF line endings; unchecked boxes are not submitted.
    submitted = {k: v.replace("\n", "\r\n") for k, v in vals.items() if v != "" or k == "prompt"}
    r = post_form(client, "/configure/catalogs/alpha_list/save", submitted, base_rev=rev, action="save")
    assert r.status_code == 303, r.text[:500]
    row = catalog_row("alpha_list")
    assert json.loads(row["definition_json"]) == definition
    assert row["config_rev"] == rev and row["epoch"] == epoch


def test_rendered_form_round_trips_from_the_html(client):
    """The values the page itself renders (not editor.form_values) submit back unchanged."""
    definition = ROUND_TRIP_DEFINITIONS[1]
    rev = add_catalog(**definition)
    html = client.get("/configure/catalogs/alpha_list").text
    form_html = html.split('id="form-tab"', 1)[1].split("</form>", 1)[0]
    data = {}
    for m in re.finditer(r'<input id="f-(\w+)" name="\1"[^>]*?value="([^"]*)"', form_html):
        data[m.group(1)] = m.group(2)
    for m in re.finditer(r'<input id="f-(\w+)" type="checkbox"[^>]*name="\1" value="on" checked', form_html):
        data[m.group(1)] = "on"
    for m in re.finditer(r'<textarea id="f-(\w+)" name="\1"[^>]*>\n(.*?)</textarea>', form_html, re.S):
        data[m.group(1)] = m.group(2)
    for m in re.finditer(r'<select id="f-(\w+)" name="\1">(.*?)</select>', form_html, re.S):
        sel = re.search(r'<option value="([^"]*)" selected', m.group(2))
        data[m.group(1)] = sel.group(1) if sel else ""
    # The refresh widget: the checked radio, its inputs and the weekday select.
    for m in re.finditer(r'<input[^>]*name="(sched_\w+)" value="([^"]*)"', form_html):
        if m.group(1) != "sched_mode":
            data[m.group(1)] = m.group(2)
    data["sched_mode"] = re.search(r'name="sched_mode" value="(\w+)" checked', form_html).group(1)
    day = re.search(r'name="sched_day"[^>]*>(.*?)</select>', form_html, re.S).group(1)
    data["sched_day"] = re.search(r'<option value="(\d)" selected', day).group(1)
    assert data["sched_mode"] == "daily" and data["sched_tz"] == "Europe/Berlin"
    import html as html_lib

    data = {k: html_lib.unescape(v) for k, v in data.items()}
    r = post_form(client, "/configure/catalogs/alpha_list/save", data, base_rev=rev, action="save")
    assert r.status_code == 303, r.text[:800]
    assert catalog_row("alpha_list")["config_rev"] == rev


def test_stale_form_is_refused(client):
    rev = add_catalog()
    vals = values_for({"prompt": PROMPT, "min_runtime": 80})
    assert (
        post_form(client, "/configure/catalogs/alpha_list/save", vals, base_rev=rev, action="save").status_code == 303
    )
    r = post_form(
        client, "/configure/catalogs/alpha_list/save", values_for({"prompt": "Other"}), base_rev=rev, action="save"
    )
    assert r.status_code == 409 and "changed elsewhere" in r.text
    assert json.loads(catalog_row("alpha_list")["definition_json"])["min_runtime"] == 80


def test_new_catalog_cannot_overwrite_an_existing_one(client):
    rev = add_catalog()
    r = post_form(client, "/configure/new", values_for({"prompt": "Other"}), catalog_id="alpha_list", action="save")
    assert r.status_code == 422 and "already exists" in r.text
    assert catalog_row("alpha_list")["config_rev"] == rev


def test_exclusion_names_are_resolved_on_save(client, tmdb):
    vals = values_for({"prompt": PROMPT, "franchise_denylist": {"collections": ["Saga Nine", "Saga Unknown"]}})
    r = post_form(client, "/configure/new", vals, catalog_id="excl", action="validate")
    assert r.status_code == 200 and "Not found on TMDB" in r.text and "Saga Unknown" in r.text
    r = post_form(client, "/configure/new", vals, catalog_id="excl", action="save")
    assert r.status_code == 303
    resolved = json.loads(catalog_row("excl")["exclusions_json"])
    assert resolved["collection_ids"] == [900]


def test_tmdb_failure_saves_nothing(client, tmdb):
    tmdb.fail_with = 503
    vals = values_for({"prompt": PROMPT, "blacklist_titles": ["Film 3"]})
    r = post_form(client, "/configure/new", vals, catalog_id="excl", action="save")
    assert r.status_code == 422 and "Couldn&#39;t look up your exclusions on TMDB" in r.text
    assert catalog_row("excl") is None


def test_disable_enable_and_delete(client):
    rev = add_catalog()
    r = client.post(
        "/configure/catalogs/alpha_list/state", data={"csrf": token(client), "state": "disabled", "base_rev": rev}
    )
    assert r.status_code == 303
    row = catalog_row("alpha_list")
    assert row["state"] == "disabled" and json.loads(row["definition_json"])["enabled"] is False
    r = client.post(
        "/configure/catalogs/alpha_list/state",
        data={"csrf": token(client), "state": "enabled", "base_rev": row["config_rev"]},
    )
    row = catalog_row("alpha_list")
    assert row["state"] == "enabled" and json.loads(row["definition_json"]) == {"prompt": PROMPT}
    r = client.post(
        "/configure/catalogs/alpha_list/state",
        data={"csrf": token(client), "state": "removed", "base_rev": row["config_rev"]},
    )
    assert r.status_code == 303 and catalog_row("alpha_list")["state"] == "removed"
    assert client.get("/configure/catalogs/alpha_list").status_code == 404


def test_export_yaml_round_trips(client):
    definition = ROUND_TRIP_DEFINITIONS[1]
    add_catalog(**definition)
    r = client.get("/configure/catalogs/alpha_list/export.yaml")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert editor.parse_definition_yaml(r.text) == definition


# --------------------------------------------------------------------------- file vs editor


async def test_file_catalog_is_read_only_until_moved(client, editor_on, tmdb):
    (editor_on / "from_file.yaml").write_text(f"prompt: |\n  {PROMPT}min_runtime: 70\n", encoding="utf-8")
    await apply_dir(editor_on, tmdb_client_factory=tmdb.client)
    row = catalog_row("from_file")
    assert row["source"] == "file"
    page = client.get("/configure/catalogs/from_file").text
    assert "/move-to-editor" in page and '<fieldset class="stack" disabled>' in page
    vals = values_for({"prompt": "Changed"})
    r = post_form(client, "/configure/catalogs/from_file/save", vals, base_rev=row["config_rev"], action="save")
    assert r.status_code == 409 and catalog_row("from_file")["config_rev"] == row["config_rev"]

    r = client.post("/configure/catalogs/from_file/move-to-editor", data={"csrf": token(client)})
    assert r.status_code == 303
    moved = catalog_row("from_file")
    assert moved["source"] == "editor"
    assert (moved["config_rev"], moved["epoch"]) == (row["config_rev"], row["epoch"])  # no epoch bump
    page = client.get("/configure/catalogs/from_file").text
    assert "is ignored now that this catalog is managed here" in page

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
    r = post_form(client, "/configure/catalogs/from_file/save", vals, base_rev=row["config_rev"], action="save")
    assert r.status_code == 303 and json.loads(catalog_row("from_file")["definition_json"])["min_runtime"] == 90


# --------------------------------------------------------------------------- dry run and rollback


def test_dry_run_is_queued_and_its_result_shown(client):
    rev = add_catalog(max_runs_per_day=1)
    r = client.post("/configure/catalogs/alpha_list/dry-run", data={"csrf": token(client)})
    assert r.status_code == 303 and r.headers["location"].endswith("msg=dry_run_queued#activity-tab")
    with db.session() as conn:
        job = conn.execute("SELECT id, kind, state, trigger FROM jobs").fetchone()
    assert (job["kind"], job["state"], job["trigger"]) == ("dry_run", "pending", "admin")
    page = client.get(r.headers["location"]).text
    assert "Test run started" in page and "Test run in progress" in page
    assert client.get("/configure/catalogs/alpha_list/status.json").json()["open_job"]["id"] == job["id"]
    r = client.post("/configure/catalogs/alpha_list/dry-run", data={"csrf": token(client)})
    assert r.headers["location"].endswith("msg=already_open#activity-tab")

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
                json.dumps({"generated": 160, "eligible": 104, "selected": 30, "<b>x</b>": 2}),
                json.dumps({"rating_low": 21, "<script>": 1}),
            ),
        )
    assert client.get("/configure/catalogs/alpha_list/status.json").json()["open_job"] is None
    page = client.get("/configure/catalogs/alpha_list").text
    assert "<b>160</b>suggested" in page and "<b>104</b>passed your rules" in page and "<b>30</b>picked" in page
    assert "rating too low · 21" in page
    assert "&lt;script&gt; · 1" in page and "<script> · 1" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


def test_dry_run_of_a_disabled_catalog_is_refused(client):
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, "alpha_list", "Alpha", {"prompt": PROMPT}, state="disabled")
    r = client.post("/configure/catalogs/alpha_list/dry-run", data={"csrf": token(client)})
    assert r.headers["location"].endswith("msg=not_enabled#activity-tab")
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
    page = client.get("/configure/catalogs/alpha_list").text
    assert f"/rollback/{ids[0]}" in page and f"/rollback/{ids[2]}" not in page  # active one has no button
    confirm = client.get(f"/configure/catalogs/alpha_list/rollback/{ids[0]}")
    assert confirm.status_code == 200 and f"Version {ids[0]} (published" in confirm.text
    epoch = catalog_row("alpha_list")["epoch"]
    r = client.post(f"/configure/catalogs/alpha_list/rollback/{ids[0]}", data={"csrf": token(client)})
    assert r.status_code == 303 and r.headers["location"].endswith("msg=rolled_back#activity-tab")
    assert _active_id() == ids[0] and catalog_row("alpha_list")["epoch"] == epoch + 1
    page = client.get(r.headers["location"]).text
    assert "That earlier version is showing again" in page and f"/rollback/{ids[1]}" not in page  # revoked now

    r = client.post(f"/configure/catalogs/alpha_list/rollback/{ids[1]}", data={"csrf": token(client)})
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


async def test_deleting_an_editor_catalog_releases_its_id_to_a_file(client, editor_on, tmdb):
    rev = add_catalog()
    (editor_on / "alpha_list.yaml").write_text(f"prompt: |\n  {PROMPT}min_runtime: 60\n", encoding="utf-8")
    report = await apply_dir(editor_on, tmdb_client_factory=tmdb.client)
    assert [r.status for r in report.files] == ["ignored"]
    page = client.get("/configure/catalogs/alpha_list").text
    assert "that file brings the catalog back" in page
    r = client.post(
        "/configure/catalogs/alpha_list/state", data={"csrf": token(client), "state": "removed", "base_rev": rev}
    )
    assert r.status_code == 303 and catalog_row("alpha_list")["state"] == "removed"
    report = await apply_dir(editor_on, tmdb_client_factory=tmdb.client)
    assert [r.status for r in report.files] == ["accepted"]
    row = catalog_row("alpha_list")
    assert (row["source"], row["state"]) == ("file", "enabled")
    assert json.loads(row["definition_json"])["min_runtime"] == 60


def test_fixture_catalog_is_read_only_without_a_move_button(client):
    add_catalog(source="fixture")
    page = client.get("/configure/catalogs/alpha_list").text
    assert "loaded as a test fixture" in page and "/move-to-editor" not in page


def test_missing_tmdb_key_is_a_validation_error(client, app, monkeypatch):
    from app.curation.tmdb_gate import TMDBClient

    monkeypatch.setattr(settings, "TMDB_READ_ACCESS_TOKEN", None)
    app.state.admin_app.state.tmdb_client_factory = TMDBClient
    vals = values_for({"prompt": PROMPT, "blacklist_titles": ["Film 3"]})
    r = post_form(client, "/configure/new", vals, catalog_id="excl", action="save")
    assert r.status_code == 422 and "Couldn&#39;t look up your exclusions on TMDB" in r.text
    assert catalog_row("excl") is None


# --------------------------------------------------------------------------- site root, addon URL


def test_site_root_redirects_to_the_editor(anon):
    r = anon.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/configure/"
    assert not any(h.lower().startswith("access-control-") for h in r.headers)


def test_site_root_is_404_with_the_editor_off(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_EDITOR", False)
    assert TestClient(create_app()).get("/").status_code == 404


def test_index_shows_the_addon_url(client, monkeypatch):
    html = client.get("/configure/").text
    assert 'value="http://testserver/c/public/manifest.json"' in html
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://catalogs.example.org/")
    monkeypatch.setattr(settings, "ADDON_ACCESS_TOKEN", "tok_0123456789abcdef")
    html = client.get("/configure/").text
    assert 'value="https://catalogs.example.org/c/tok_0123456789abcdef/manifest.json"' in html
    assert 'data-copy="manifest-url"' in html


def test_addon_url_is_on_the_index_only(client, monkeypatch):
    monkeypatch.setattr(settings, "ADDON_ACCESS_TOKEN", "tok_0123456789abcdef")
    add_catalog()
    for path in ["/configure/new", "/configure/catalogs/alpha_list", "/configure/catalogs/alpha_list/status.json"]:
        assert "tok_0123456789abcdef" not in client.get(path).text


# --------------------------------------------------------------------------- EDITOR_AUTH=proxy

USER = {"Remote-User": "owner"}


def ptoken(client) -> str:
    """The CSRF token in proxy mode (no logout form, so read it from the New page)."""
    return csrf_of(client.get("/configure/new").text)


@pytest.fixture
def proxy_on(editor_on, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "EDITOR_AUTH", "proxy")
    monkeypatch.setattr(settings, "EDITOR_PROXY_USER_HEADER", "Remote-User")
    monkeypatch.setattr(settings, "SESSION_SECRET", None)
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(settings, "DATABASE_PATH", str(tmp_path / "proxy.sqlite"))
    admin_mod._FILE_KEYS.clear()
    return tmp_path


@pytest.fixture
def proxy_app(proxy_on, tmdb):
    application = create_app()
    application.state.admin_app.state.tmdb_client_factory = tmdb.client
    return application


@pytest.fixture
def proxied(proxy_app):
    return TestClient(proxy_app, follow_redirects=False, headers=USER)


def test_proxy_mode_starts_without_password_or_secret(proxy_app):
    assert proxy_app is not None


def test_proxy_mode_still_rejects_a_short_secret_when_one_is_set(proxy_on, monkeypatch):
    monkeypatch.setattr(settings, "SESSION_SECRET", "short")
    with pytest.raises(admin_mod.EditorConfigError):
        create_app()


def test_proxy_mode_needs_a_header_name(proxy_on, monkeypatch):
    monkeypatch.setattr(settings, "EDITOR_PROXY_USER_HEADER", " ")
    with pytest.raises(admin_mod.EditorConfigError):
        create_app()


def test_proxy_mode_fails_closed_without_the_user_header(proxy_app):
    add_catalog()
    c = TestClient(proxy_app, follow_redirects=False)
    for method, path in [
        ("GET", "/configure/"),
        ("GET", "/configure/login"),
        ("POST", "/configure/login"),
        ("GET", "/configure/catalogs/alpha_list"),
        ("GET", "/configure/static/admin.css"),
        ("POST", "/configure/catalogs/alpha_list/dry-run"),
        ("GET", "/configure/nope"),
    ]:
        for headers in ({}, {"Remote-User": ""}, {"Remote-User": "   "}):
            r = c.request(method, path, headers=headers)
            assert r.status_code == 403, (method, path, headers)
            assert r.headers["cache-control"] == "no-store"


def test_proxy_mode_signed_in_user_sees_the_editor(proxied):
    add_catalog()
    r = proxied.get("/configure/")
    assert r.status_code == 200
    assert "Signed in as" not in r.text
    assert "/configure/logout" not in r.text and "password" not in r.text.lower()
    assert proxied.get("/configure/catalogs/alpha_list").status_code == 200
    assert not any(c.startswith(admin_mod.SESSION_COOKIE) for c in r.headers.get_list("set-cookie"))


def test_proxy_mode_has_no_login_or_logout(proxied):
    r = proxied.get("/configure/login")
    assert r.status_code == 303 and r.headers["location"] == "/configure/"
    assert proxied.post("/configure/login", data={"password": "x"}).status_code == 404
    assert proxied.post("/configure/logout", data={"csrf": ptoken(proxied)}).status_code == 404


def test_proxy_mode_header_name_is_configurable(proxy_on, tmdb, monkeypatch):
    monkeypatch.setattr(settings, "EDITOR_PROXY_USER_HEADER", "X-Forwarded-User")
    c = TestClient(create_app(), follow_redirects=False)
    assert c.get("/configure/", headers=USER).status_code == 403
    assert c.get("/configure/", headers={"X-Forwarded-User": "owner"}).status_code == 200


def test_proxy_mode_posts_still_need_csrf_and_same_origin(proxied, proxy_app):
    add_catalog()
    path = "/configure/catalogs/alpha_list/dry-run"
    assert proxied.post(path, data={}).status_code == 403
    assert proxied.post(path, data={"csrf": "forged"}).status_code == 403
    assert proxied.post(path, data={"csrf": ptoken(proxied)}, headers=ORIGIN).status_code == 403
    r = proxied.post(path, data={"csrf": ptoken(proxied)}, headers={"Origin": "http://testserver"})
    assert r.status_code == 303


def test_proxy_mode_csrf_token_is_bound_to_the_user(proxied, proxy_app):
    add_catalog()
    other = TestClient(proxy_app, follow_redirects=False, headers={"Remote-User": "someone-else"})
    r = proxied.post("/configure/catalogs/alpha_list/dry-run", data={"csrf": ptoken(other)})
    assert r.status_code == 403


def test_proxy_mode_key_file_is_private_and_shared(proxied, proxy_on, proxy_app):
    first = ptoken(proxied)
    key_file = proxy_on / "editor-csrf.key"
    assert key_file.is_file() and (key_file.stat().st_mode & 0o777) == 0o600
    admin_mod._FILE_KEYS.clear()  # as a second uvicorn worker would start: it reads the same file
    assert ptoken(proxied) == first


def test_proxy_mode_uses_session_secret_when_set(proxy_on, monkeypatch, tmdb):
    monkeypatch.setattr(settings, "SESSION_SECRET", SECRET)
    c = TestClient(create_app(), follow_redirects=False, headers=USER)
    assert c.get("/configure/").status_code == 200
    assert not (proxy_on / "editor-csrf.key").exists()


def test_list_sizes_are_shown_as_numbers(client):
    """Jinja's `x.items` is dict.items, not the "items" key: pages must show the count."""
    add_catalog()
    with db.session() as conn, db.transaction(conn):
        rev = catalog_row("alpha_list")["config_rev"]
        publish_snapshot(
            conn, "alpha_list", [SnapshotItem(f"tt{9000000 + i}", f"Film {i}", 2001) for i in range(3)], config_rev=rev
        )
        publish_snapshot(
            conn, "alpha_list", [SnapshotItem(f"tt{9100000 + i}", f"Film {i}", 2001) for i in range(4)], config_rev=rev
        )
    pages = [client.get(p).text for p in ("/configure/", "/configure/catalogs/alpha_list")]
    pages.append(client.get("/configure/catalogs/alpha_list/rollback/1").text)
    for html in pages:
        assert "built-in method" not in html
    assert "4 movies" in pages[0]
    assert "<td>3</td>" in pages[1] and "<td>4</td>" in pages[1]
    assert "3 movies" in pages[2]


# --------------------------------------------------------------------------- simple form (PLAN.md section 26)


def test_new_catalog_needs_only_a_name_and_a_description(client):
    page = client.get("/configure/new").text
    assert 'name="catalog_id"' not in page  # derived from the name
    r = client.post(
        "/configure/new",
        data={
            "csrf": token(client),
            "mode": "form",
            "name": "Sunday Comfort!",
            "prompt": "Warm, funny films.",
            "enabled": "on",  # the switch starts on; a browser submits it even with Advanced closed
            "action": "save",
        },
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/configure/catalogs/sunday_comfort")
    saved = json.loads(catalog_row("sunday_comfort")["definition_json"])
    assert saved == {"name": "Sunday Comfort!", "prompt": "Warm, funny films."}
    # The same name again gets its own id.
    r = client.post(
        "/configure/new",
        data={
            "csrf": token(client),
            "mode": "form",
            "name": "Sunday Comfort",
            "prompt": "More.",
            "enabled": "on",
            "action": "save",
        },
    )
    assert r.headers["location"].startswith("/configure/catalogs/sunday_comfort_2")


def test_plain_description_previews_with_list_size_and_rules(client):
    r = client.post(
        "/configure/new",
        data={
            "csrf": token(client),
            "mode": "form",
            "name": "Alpha",
            "prompt": "Films for category alpha.",
            "min_runtime": "80",
            "action": "validate",
        },
    )
    assert r.status_code == 200 and "Looks good" in r.text
    assert "Films for category alpha." in r.text and "80" in r.text


@pytest.mark.parametrize(
    "widget, expected",
    [
        ({"sched_mode": "manual"}, None),
        (
            {"sched_mode": "daily", "sched_time": "06:30", "sched_tz": "Europe/Berlin"},
            {"cron": "30 6 * * *", "timezone": "Europe/Berlin"},
        ),
        (
            {"sched_mode": "weekly", "sched_time": "18:00", "sched_day": "5", "sched_tz": ""},
            {"cron": "0 18 * * 5", "timezone": "UTC"},
        ),
        (
            {"sched_mode": "custom", "sched_cron": "0  */6 * * *", "sched_tz": "UTC"},
            {"cron": "0 */6 * * *", "timezone": "UTC"},
        ),
    ],
)
def test_refresh_widget_builds_the_schedule(client, widget, expected):
    r = client.post(
        "/configure/new",
        data={"csrf": token(client), "mode": "form", "name": "Alpha", "prompt": PROMPT, "action": "save", **widget},
    )
    assert r.status_code == 303, r.text[:400]
    assert json.loads(catalog_row("alpha")["definition_json"]).get("schedule") == expected


@pytest.mark.parametrize(
    "stored, mode",
    [
        ({"cron": "0 6 * * *"}, "daily"),
        ({"cron": "00 06 * * *", "timezone": "UTC"}, "daily"),
        ({"cron": "15 20 * * 7", "timezone": "Asia/Tokyo"}, "weekly"),
        ({"cron": "0 6 1 * *", "timezone": "UTC"}, "custom"),
    ],
)
def test_refresh_widget_keeps_a_stored_schedule_exactly(stored, mode):
    values = editor.schedule_values(stored)
    assert values["sched_mode"] == mode
    assert editor.schedule_from_form(values, stored) == stored


@pytest.mark.parametrize(
    "widget, message",
    [
        ({"sched_mode": "daily", "sched_time": "25:00"}, "time like 06:00"),
        ({"sched_mode": "weekly", "sched_time": "06:00", "sched_day": "9"}, "choose a weekday"),
        ({"sched_mode": "custom", "sched_cron": ""}, "cron expression"),
        ({"sched_mode": "hourly"}, "choose manually"),
    ],
)
def test_refresh_widget_errors_are_shown(client, widget, message):
    r = client.post(
        "/configure/new",
        data={"csrf": token(client), "mode": "form", "name": "Alpha", "prompt": PROMPT, "action": "save", **widget},
    )
    assert r.status_code == 422 and message in r.text and catalog_row("alpha") is None


def test_refresh_now_queues_a_real_refresh(client):
    add_catalog()
    r = client.post("/configure/catalogs/alpha_list/refresh", data={"csrf": token(client)})
    assert r.status_code == 303 and r.headers["location"].endswith("msg=refresh_queued#activity-tab")
    with db.session() as conn:
        job = conn.execute("SELECT kind, trigger FROM jobs").fetchone()
    assert (job["kind"], job["trigger"]) == ("refresh", "admin")
    assert client.post("/configure/catalogs/alpha_list/refresh", data={}).status_code == 403  # CSRF as usual


def test_basic_settings_come_first_and_the_rest_is_advanced(client):
    page = client.get("/configure/new").text
    simple, advanced = page.split('class="advanced"', 1)
    for name in editor.BASIC_FIELDS:
        assert f'name="{name}"' in simple, name
    for name in ("focus", "weekday_focus", "categories", "enabled", "temperature"):
        assert f'name="{name}"' in advanced and f'name="{name}"' not in simple, name
    assert "What kind of movies do you want?" in simple and "catalog id" not in page.lower()


def test_new_field_still_appears_in_advanced(monkeypatch):
    """A definition field added later (milestone 7) shows up under Advanced by itself."""
    from pydantic.fields import FieldInfo

    fields = dict(editor.CatalogDefinition.model_fields)
    fields["shiny_new_option"] = FieldInfo(annotation=bool, default=False)
    monkeypatch.setattr(editor.CatalogDefinition, "model_fields", fields)
    groups = dict(editor.advanced_groups())
    assert "shiny_new_option" in [f.name for f in groups["Other"]]


def test_pages_show_the_logo_and_no_signed_in_label(client):
    add_catalog()
    for path in ("/configure/", "/configure/new", "/configure/catalogs/alpha_list"):
        html = client.get(path).text
        assert 'src="/static/logo_256.png"' in html and "Signed in as" not in html


# --------------------------------------------------------------------------- dashboard, pickers (PLAN.md section 27)


def test_dashboard_cards_can_run_test_edit_and_delete(client):
    add_catalog()
    page = client.get("/configure/").text
    assert "/configure/catalogs/alpha_list/refresh" in page and "▶ Run now" in page
    assert "/configure/catalogs/alpha_list/dry-run" in page and "Test run" in page
    assert 'value="removed"' in page and "data-confirm=" in page
    r = client.post(
        "/configure/catalogs/alpha_list/state",
        data={"csrf": token(client), "state": "removed", "base_rev": catalog_row("alpha_list")["config_rev"]},
    )
    assert r.status_code == 303 and "msg=deleted" in r.headers["location"]
    assert catalog_row("alpha_list")["state"] == "removed"


def test_dragging_reorders_without_touching_definitions(client):
    revs = {cid: add_catalog(cid) for cid in ("alpha_one", "beta_two", "gamma_three")}
    epochs = {cid: catalog_row(cid)["epoch"] for cid in revs}
    r = client.post("/configure/order", data={"csrf": token(client), "order": "gamma_three,alpha_one,bad id,beta_two"})
    assert r.status_code == 200 and r.json()["count"] == 3
    page = client.get("/configure/").text
    assert page.index('data-id="gamma_three"') < page.index('data-id="alpha_one"') < page.index('data-id="beta_two"')
    for cid in revs:
        assert catalog_row(cid)["config_rev"] == revs[cid] and catalog_row(cid)["epoch"] == epochs[cid]
    assert client.post("/configure/order", data={"order": "beta_two"}).status_code == 403  # CSRF as usual


def test_saving_keeps_the_dashboard_order(client):
    add_catalog("alpha_one")
    add_catalog("beta_two")
    client.post("/configure/order", data={"csrf": token(client), "order": "beta_two,alpha_one"})
    rev = catalog_row("alpha_one")["config_rev"]
    r = post_form(
        client, "/configure/catalogs/alpha_one/save", values_for({"prompt": "Changed."}), base_rev=rev, action="save"
    )
    assert r.status_code == 303
    assert catalog_row("alpha_one")["position"] == 1 and catalog_row("beta_two")["position"] == 0


def test_hidden_fields_are_kept_on_save(client):
    rev = add_catalog(position=4, blacklist_tmdb_ids=[11, 12])
    vals = values_for({"prompt": PROMPT, "position": 4, "blacklist_tmdb_ids": [11, 12]})
    vals.pop("position", None)
    vals.pop("blacklist_tmdb_ids", None)
    page = client.get("/configure/catalogs/alpha_list").text
    assert 'name="position"' not in page and 'name="blacklist_tmdb_ids"' not in page
    r = post_form(client, "/configure/catalogs/alpha_list/save", vals, base_rev=rev, action="save")
    assert r.status_code == 303 and catalog_row("alpha_list")["config_rev"] == rev


def test_movie_suggestions_come_from_tmdb(client):
    r = client.get("/configure/suggest", params={"kind": "movie", "q": "Film 3"})
    assert r.status_code == 200 and r.json()[0]["value"].startswith("Film 3 (")
    assert client.get("/configure/suggest", params={"kind": "movie", "q": "x"}).json() == []  # too short
    assert client.get("/configure/suggest", params={"kind": "nope", "q": "Film 3"}).json() == []
    r = client.get("/configure/suggest", params={"kind": "collection", "q": "Saga Nine"})
    assert r.json() == [{"label": "Saga Nine Collection", "value": "Saga Nine Collection"}]


def test_suggestions_need_a_session(anon):
    assert anon.get("/configure/suggest", params={"kind": "movie", "q": "Film 3"}).status_code == 303


def test_model_suggestions_filter_the_provider_list(client, monkeypatch):
    async def fake_ids():
        return ["google/gemini-flash-lite", "google/gemini-pro", "openai/gpt-mini"]

    monkeypatch.setattr(admin_mod, "_model_ids", fake_ids)
    r = client.get("/configure/suggest", params={"kind": "model", "q": "gemini lite"})
    assert r.json() == [{"label": "google/gemini-flash-lite", "value": "google/gemini-flash-lite"}]


def test_failed_suggestions_are_just_empty(client, monkeypatch):
    async def boom():
        raise RuntimeError("provider down")

    monkeypatch.setattr(admin_mod, "_model_ids", boom)
    assert client.get("/configure/suggest", params={"kind": "model", "q": "gemini"}).json() == []


def test_test_run_picks_are_shown(client):
    rev = add_catalog()
    with db.session() as conn, db.transaction(conn):
        conn.execute(
            "INSERT INTO runs (catalog_id, config_rev, epoch, trigger, dry_run, started_at, status, stage_counts_json, "
            "reject_reasons_json, model_calls, preview_json) VALUES ('alpha_list', ?, 0, 'admin', 1, ?, 'ok', ?, ?, 2, ?)",
            (
                rev,
                db.now_ms(),
                json.dumps({"generated": 9, "eligible": 7, "selected": 2, "dropped_rows": 0, "model_calls_ok": 2}),
                json.dumps({}),
                json.dumps([["Film <1>", 1999, "tt0000001"], ["Film 2", None, "tt0000002"]]),
            ),
        )
    page = client.get("/configure/catalogs/alpha_list").text
    assert "2 movies it picked" in page and "(not published)" in page
    assert "Film &lt;1&gt;" in page and "1999" in page and "Film 2" in page
    assert "2 AI requests" in page and "dropped_rows" not in page and "model_calls_ok" not in page


def test_settings_page_offers_pickers_and_no_yaml_tab(client):
    add_catalog()
    page = client.get("/configure/catalogs/alpha_list").text
    assert 'href="#yaml-tab"' not in page and "Edit as YAML" not in page
    for name, widget in (
        ("languages", "chips"),
        ("excluded_genre_ids", "chips"),
        ("blacklist_titles", "chips"),
        ("franchise_denylist", "denylist"),
        ("weekday_focus", "weekdays"),
        ("decade_min", "pairs"),
        ("model", "suggest"),
    ):
        assert re.search(rf'name="{name}"[^>]*data-widget="{widget}"', page, re.S), name
    assert "&#34;English&#34;" in page or '"English"' in page  # language names, not codes
    assert "Cancel" in page and "👁 Preview" in page and "⚗ Test run" in page and "▶ Run now" in page


def test_slider_ranges_are_sensible():
    by_name = {f.name: f for f in editor.basic_fields()}
    assert by_name["year_min"].minimum == 1900 and by_name["year_min"].maximum <= 2100
    assert by_name["min_runtime"].maximum == 240
    adv = {f.name: f for _, fs in editor.advanced_groups() for f in fs}
    assert adv["history_window_runs"].maximum == 30 and not adv["max_prompt_chars"].slider


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("year_min: Input should be a valid integer, unable to parse", "Released from: enter a whole number"),
        (
            "Value error, year_min must not be greater than year_max",
            "“Released from” must not be greater than “Released until”",
        ),
        ("target_count: Input should be less than or equal to 250", "Movies in the catalog: must be at most 250"),
        (
            "Value error, on_shortfall: publish_partial needs min_accept",
            "When a refresh comes up short: “Publish what was found” needs “Smallest catalog worth publishing”",
        ),
        ("something else entirely", "something else entirely"),
    ],
)
def test_errors_are_in_plain_language(raw, expected):
    assert admin_mod.friendly_error(raw) == expected


def test_notices_are_short_and_plain(client):
    add_catalog()
    r = client.get("/configure/catalogs/alpha_list?msg=saved").text
    assert "Saved." in r and "definition" not in r.split("Saved.")[1][:40]
