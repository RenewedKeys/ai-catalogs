"""
Browser editor (PLAN.md section 21): ``/configure``, a sub-app with no CORS middleware, mounted in
``create_app()`` before the Stremio catch-all.

Off unless ``ENABLE_EDITOR`` is true; then every path answers 404, as if ``/configure`` did not
exist. ``/configure`` is the usual Stremio addon settings path, so a reverse proxy rule that
protects addon settings pages covers the whole editor (PLAN.md section 23).

``EDITOR_AUTH=password`` (default): ``SESSION_SECRET`` (>= 32 bytes) and ``ADMIN_PASSWORD``
(>= 12 characters) are required; ``check_editor_settings()`` makes ``create_app()`` fail at
startup without them.

``EDITOR_AUTH=proxy``: no login of its own. The reverse proxy's login (e.g. Authelia behind
Traefik forward-auth) sets ``EDITOR_PROXY_USER_HEADER`` (default ``Remote-User``); a request
without it is refused (403), so the editor fails closed if the proxy rule is ever removed. Only
safe when the container is reachable through that proxy alone. CSRF tokens are then bound to the
user name and signed with ``SESSION_SECRET`` if set, otherwise with a random key kept next to the
database (``editor-csrf.key``).

Auth: one password, a login form, then a stateless session cookie signed with HMAC-SHA256
(HttpOnly, SameSite=Strict, Path=/configure, Secure when ``PUBLIC_BASE_URL`` is https, 12 h).
Logout clears the cookie; rotating ``SESSION_SECRET`` ends every session. Every POST carries a
CSRF token (HMAC of the session id; the login form's is bound to a short-lived pre-login
cookie) and, when the browser sends one, an ``Origin`` of this site. Failed logins are limited
in SQLite (shared by all uvicorn workers): 5 per client and 20 overall per 15 minutes.

Without a session: GET -> redirect to the login page, anything else -> 401, for every path
(unknown ones too), so nothing about the editor leaks before login. Every response carries
``no-store``, a strict CSP (no inline script or style) and anti-framing headers.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from app.api.curated import PUBLIC_SEGMENT
from app.api.ops import enqueue
from app.core.config import settings
from app.curation import editor
from app.curation.definitions import DefinitionError
from app.curation.status import catalog_status, iso
from app.curation.tmdb_gate import TMDBClient
from app.store import db
from app.store.catalogs import CATALOG_ID_RE
from app.store.maintenance import MaintenanceError, rollback

PREFIX = "/configure"
SESSION_COOKIE = "aic_admin"
PRELOGIN_COOKIE = "aic_admin_pre"
SESSION_TTL_S = 12 * 3600
PRELOGIN_TTL_S = 3600
MIN_SECRET_BYTES = 32
MIN_PASSWORD_CHARS = 12
LOGIN_WINDOW_MS = 15 * 60 * 1000
LOGIN_MAX_PER_CLIENT = 5
LOGIN_MAX_TOTAL = 20
SNAPSHOTS_SHOWN = 20

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "admin"
STATIC_FILES = {"admin.js": "text/javascript", "admin.css": "text/css"}
PUBLIC_PATHS = ("/login", "/login/", "/static/admin.css")  # the login page and its stylesheet

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

# Fixed notices, selected by a key in the redirect URL (never free text from the URL).
NOTICES = {
    "saved": "Saved. The new definition is live.",
    "created": "Catalog created.",
    "moved": "The catalog is now managed in the editor. Delete or rename its file in catalogs.d.",
    "enabled": "Catalog enabled.",
    "disabled": "Catalog disabled.",
    "dry_run_queued": "Dry run queued. The worker runs it; this page updates when it ends.",
    "already_open": "The catalog already has an open job; wait for it to finish.",
    "daily_limit": "max_runs_per_day is reached for this catalog.",
    "not_enabled": "Only an enabled catalog can run.",
    "rolled_back": "Rolled back. That list is being served again.",
    "deleted": "Catalog deleted (marked removed).",
}


class EditorConfigError(RuntimeError):
    """ENABLE_EDITOR is on but the editor's settings are unusable (startup refuses)."""


def proxy_mode() -> bool:
    return settings.EDITOR_AUTH == "proxy"


def check_editor_settings(enable_legacy: Optional[bool] = None) -> None:
    if not settings.ENABLE_EDITOR:
        return
    if settings.ENABLE_LEGACY if enable_legacy is None else enable_legacy:
        raise EditorConfigError("ENABLE_EDITOR and ENABLE_LEGACY both use /configure; turn one of them off")
    secret = settings.SESSION_SECRET or ""
    if proxy_mode():
        if not settings.EDITOR_PROXY_USER_HEADER.strip():
            raise EditorConfigError("EDITOR_AUTH=proxy: EDITOR_PROXY_USER_HEADER must name a header")
        if secret and len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
            raise EditorConfigError(f"SESSION_SECRET, when set, must be at least {MIN_SECRET_BYTES} bytes")
        return
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise EditorConfigError(f"ENABLE_EDITOR is set: SESSION_SECRET must be at least {MIN_SECRET_BYTES} bytes")
    if len(settings.ADMIN_PASSWORD or "") < MIN_PASSWORD_CHARS:
        raise EditorConfigError(
            f"ENABLE_EDITOR is set: ADMIN_PASSWORD must be at least {MIN_PASSWORD_CHARS} characters"
        )


# ------------------------------------------------------------------ signing


_FILE_KEYS: dict[str, bytes] = {}


def _file_key() -> bytes:
    """A random signing key kept next to the database (EDITOR_AUTH=proxy without SESSION_SECRET).

    Created once with O_EXCL, so every uvicorn worker ends up reading the same key."""
    path = Path(settings.DATABASE_PATH).resolve().parent / "editor-csrf.key"
    cached = _FILE_KEYS.get(str(path))
    if cached:
        return cached
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = b""
        for _ in range(50):  # another worker may be writing it right now
            key = path.read_bytes().strip()
            if len(key) >= MIN_SECRET_BYTES:
                break
            time.sleep(0.02)
        if len(key) < MIN_SECRET_BYTES:
            raise EditorConfigError(f"{path} holds no usable key; delete it to have a new one made") from None
    else:
        key = secrets.token_hex(32).encode()
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    _FILE_KEYS[str(path)] = key
    return key


def _key() -> bytes:
    if settings.SESSION_SECRET:
        return settings.SESSION_SECRET.encode("utf-8")
    if proxy_mode():
        return _file_key()
    return b""


def _mac(purpose: str, data: bytes) -> bytes:
    return hmac.new(_key(), purpose.encode() + b"|" + data, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(purpose: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"{_b64(body)}.{_b64(_mac(purpose, body))}"


def unsign(purpose: str, token: Optional[str], now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The payload of a valid, unexpired token signed for ``purpose``; otherwise ``None``."""
    if not token or token.count(".") != 1:
        return None
    body_text, mac_text = token.split(".")
    try:
        body, mac = _unb64(body_text), _unb64(mac_text)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(mac, _mac(purpose, body)):
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("sid"), str):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= (time.time() if now is None else now):
        return None
    return payload


def csrf_token(sid: str) -> str:
    return _b64(_mac("csrf", sid.encode()))


def password_ok(presented: str) -> bool:
    expected = settings.ADMIN_PASSWORD or ""
    if not expected:
        return False
    return hmac.compare_digest(_mac("password", presented.encode("utf-8")), _mac("password", expected.encode("utf-8")))


def _secure() -> bool:
    return (settings.PUBLIC_BASE_URL or "").lower().startswith("https://")


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path=PREFIX, httponly=True, samesite="strict", secure=_secure())


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path=PREFIX, httponly=True, samesite="strict", secure=_secure())


def session_of(request: Request) -> Optional[dict[str, Any]]:
    if proxy_mode():
        user = (request.headers.get(settings.EDITOR_PROXY_USER_HEADER) or "").strip()
        # The CSRF token is the MAC of "proxy:<user>": stable per user, useless to anyone else.
        return {"sid": f"proxy:{user}", "user": user} if user else None
    return unsign("session", request.cookies.get(SESSION_COOKIE))


def manifest_url(request: Request) -> str:
    base = (settings.PUBLIC_BASE_URL or f"{request.url.scheme}://{request.url.netloc}").rstrip("/")
    return f"{base}/c/{settings.ADDON_ACCESS_TOKEN or PUBLIC_SEGMENT}/manifest.json"


# ------------------------------------------------------------------ login rate limit


def _client(request: Request) -> str:
    return (request.client.host if request.client else None) or "unknown"


def login_blocked(client: str) -> bool:
    now = db.now_ms()
    with db.session() as conn, db.transaction(conn):
        conn.execute("DELETE FROM admin_login_failures WHERE at < ?", (now - LOGIN_WINDOW_MS,))
        total = conn.execute("SELECT COUNT(*) FROM admin_login_failures").fetchone()[0]
        mine = conn.execute("SELECT COUNT(*) FROM admin_login_failures WHERE client = ?", (client,)).fetchone()[0]
    return mine >= LOGIN_MAX_PER_CLIENT or total >= LOGIN_MAX_TOTAL


def record_login_failure(client: str) -> None:
    with db.session() as conn, db.transaction(conn):
        conn.execute("INSERT INTO admin_login_failures (client, at) VALUES (?, ?)", (client, db.now_ms()))


# ------------------------------------------------------------------ CSRF


def _origin_ok(request: Request) -> bool:
    """A cross-site browser POST carries a foreign Origin; no header (old clients) is allowed."""
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if not referer:
            return True
        origin = referer
    parts = urlsplit(origin)
    if not parts.netloc:
        return False  # "null" or garbage
    allowed = {request.headers.get("host", "")}
    if settings.PUBLIC_BASE_URL:
        allowed.add(urlsplit(settings.PUBLIC_BASE_URL).netloc)
    return parts.netloc in allowed


async def verified_form(request: Request, sid: str) -> dict[str, str]:
    """The POSTed form, after the CSRF token and Origin checks (403 otherwise)."""
    form = await request.form()
    token = form.get("csrf")
    if not isinstance(token, str) or not hmac.compare_digest(token.encode(), csrf_token(sid).encode()):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    if not _origin_ok(request):
        raise HTTPException(status_code=403, detail="cross-site request refused")
    return {k: v for k, v in form.items() if isinstance(v, str)}


# ------------------------------------------------------------------ data


def _catalogs() -> list[dict[str, Any]]:
    now = db.now_ms()
    with db.session() as conn:
        rows = conn.execute(
            "SELECT catalog_id FROM catalog_defs WHERE state != 'removed' ORDER BY position, catalog_id"
        ).fetchall()
        return [catalog_status(conn, r["catalog_id"], now) for r in rows]


def _load(catalog_id: str) -> Optional[dict[str, Any]]:
    with db.session() as conn:
        row = conn.execute(
            "SELECT catalog_id, name, config_rev, definition_json, state, source, last_error FROM catalog_defs "
            "WHERE catalog_id = ? AND state != 'removed'",
            (catalog_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["definition"] = json.loads(row["definition_json"])
        out["status"] = catalog_status(conn, catalog_id, db.now_ms())
        snaps = conn.execute(
            """
            SELECT s.id, s.created_at, s.item_count, s.status, s.metas_json IS NOT NULL AS has_payload,
                   a.id AS activation_id, a.revoked_at
            FROM snapshots s LEFT JOIN activations a ON a.snapshot_id = s.id
            WHERE s.catalog_id = ? ORDER BY s.id DESC LIMIT ?
            """,
            (catalog_id, SNAPSHOTS_SHOWN),
        ).fetchall()
    out["snapshots"] = [
        {
            "id": s["id"],
            "created_at": iso(s["created_at"]),
            "items": s["item_count"],
            "status": s["status"],
            "has_payload": bool(s["has_payload"]),
            "revoked": s["revoked_at"] is not None,
            "eligible": s["status"] != "active"
            and bool(s["has_payload"])
            and s["activation_id"] is not None
            and s["revoked_at"] is None,
        }
        for s in snaps
    ]
    return out


def _file_exists(catalog_id: str) -> Optional[str]:
    directory = Path(settings.CATALOGS_DIR)
    for suffix in (".yaml", ".yml"):
        if (directory / f"{catalog_id}{suffix}").is_file():
            return f"{catalog_id}{suffix}"
    return None


def _rollback(catalog_id: str, snapshot_id: int) -> None:
    with db.session() as conn:
        rollback(conn, catalog_id, snapshot_id)


# ------------------------------------------------------------------ app


def create_admin_app(tmdb_client_factory: Optional[Callable[[], TMDBClient]] = None) -> FastAPI:
    admin = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    admin.state.tmdb_client_factory = tmdb_client_factory or TMDBClient
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=select_autoescape(default=True))

    def page(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        sess = getattr(request.state, "session", None)
        context.setdefault("csrf", csrf_token(sess["sid"]) if sess else "")
        context.setdefault("logged_in", sess is not None)
        context["prefix"] = PREFIX
        context["proxy_auth"] = proxy_mode()
        context["user"] = sess.get("user") if sess else None
        return HTMLResponse(env.get_template(template).render(**context), status_code=status_code)

    def redirect(path: str, msg: Optional[str] = None) -> RedirectResponse:
        return RedirectResponse(PREFIX + path + (f"?msg={msg}" if msg else ""), status_code=303)

    def notice(request: Request) -> Optional[str]:
        return NOTICES.get(request.query_params.get("msg", ""))

    @admin.middleware("http")
    async def guard(request: Request, call_next):
        # Before routing, so unknown paths and wrong methods leak nothing either.
        if not settings.ENABLE_EDITOR:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        path = request.url.path[len(PREFIX) :] if request.url.path.startswith(PREFIX) else request.url.path
        sess = session_of(request)
        request.state.session = sess
        if proxy_mode() and sess is None:
            # Fail closed: no user header means the request did not pass the proxy's login.
            response = PlainTextResponse(
                "Sign-in required: this editor relies on the reverse proxy's login (EDITOR_AUTH=proxy), "
                "and this request did not come through it.",
                status_code=403,
            )
        elif proxy_mode() and path in ("/login", "/login/", "/logout"):
            response = (
                redirect("/") if request.method == "GET" else JSONResponse({"detail": "Not Found"}, status_code=404)
            )
        elif sess is None and path not in PUBLIC_PATHS:
            if request.method == "GET":
                response = RedirectResponse(PREFIX + "/login", status_code=303)
            else:
                response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
        else:
            response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response

    @admin.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    # ---------------------------------------------------------------- login / logout

    def _prelogin(request: Request) -> tuple[str, Optional[str]]:
        """(pre-login sid, new cookie value or None if the current one is valid)."""
        pre = unsign("prelogin", request.cookies.get(PRELOGIN_COOKIE))
        if pre is not None:
            return pre["sid"], None
        sid = secrets.token_urlsafe(18)
        return sid, sign("prelogin", {"sid": sid, "exp": int(time.time()) + PRELOGIN_TTL_S})

    def _login_page(request: Request, error: Optional[str] = None, status_code: int = 200) -> HTMLResponse:
        sid, cookie = _prelogin(request)
        response = page(request, "login.html", status_code, csrf=csrf_token(sid), error=error, logged_in=False)
        if cookie:
            _set_cookie(response, PRELOGIN_COOKIE, cookie, PRELOGIN_TTL_S)
        return response

    @admin.get("/login")
    async def login_form(request: Request):
        if request.state.session is not None:
            return redirect("/")
        return _login_page(request)

    @admin.post("/login")
    async def login(request: Request):
        pre = unsign("prelogin", request.cookies.get(PRELOGIN_COOKIE))
        if pre is None:
            return _login_page(request, "Your login form expired; try again.", 403)
        form = await verified_form(request, pre["sid"])
        client = _client(request)
        if await run_in_threadpool(login_blocked, client):
            return _login_page(request, "Too many failed logins. Try again in 15 minutes.", 429)
        if not password_ok(form.get("password", "")):
            await run_in_threadpool(record_login_failure, client)
            return _login_page(request, "Wrong password.", 401)
        now = int(time.time())
        response = redirect("/")
        token = sign("session", {"sid": secrets.token_urlsafe(18), "iat": now, "exp": now + SESSION_TTL_S})
        _set_cookie(response, SESSION_COOKIE, token, SESSION_TTL_S)
        _clear_cookie(response, PRELOGIN_COOKIE)
        return response

    @admin.post("/logout")
    async def logout(request: Request):
        await verified_form(request, request.state.session["sid"])
        response = redirect("/login")
        _clear_cookie(response, SESSION_COOKIE)
        return response

    # ---------------------------------------------------------------- static

    @admin.get("/static/{name}")
    async def static(name: str):
        if name not in STATIC_FILES:
            raise HTTPException(status_code=404, detail="Not Found")
        return Response((TEMPLATES_DIR / name).read_bytes(), media_type=STATIC_FILES[name])

    # ---------------------------------------------------------------- pages

    @admin.get("/")
    async def index(request: Request):
        catalogs = await run_in_threadpool(_catalogs)
        return page(
            request, "index.html", catalogs=catalogs, notice=notice(request), manifest_url=manifest_url(request)
        )

    def _editor_page(
        request: Request,
        catalog: Optional[dict[str, Any]],
        *,
        catalog_id: str = "",
        values: Optional[dict[str, str]] = None,
        yaml_text: Optional[str] = None,
        mode: str = "form",
        errors: Optional[list[str]] = None,
        checked: Optional[editor.CheckResult] = None,
        status_code: int = 200,
        message: Optional[str] = None,
    ) -> HTMLResponse:
        definition = catalog["definition"] if catalog else {}
        return page(
            request,
            "catalog.html",
            status_code,
            catalog=catalog,
            catalog_id=catalog["catalog_id"] if catalog else catalog_id,
            groups=editor.grouped_fields(),
            values=values if values is not None else editor.form_values(definition),
            yaml_text=yaml_text if yaml_text is not None else editor.definition_yaml(definition),
            mode=mode,
            errors=errors or [],
            checked=checked,
            notice=message or notice(request),
            file_present=_file_exists(catalog["catalog_id"]) if catalog else None,
            editable=catalog is None or catalog["source"] == "editor",
        )

    @admin.get("/new")
    async def new_catalog(request: Request):
        return _editor_page(request, None)

    @admin.get("/catalogs/{catalog_id}")
    async def show_catalog(request: Request, catalog_id: str):
        catalog = await run_in_threadpool(_load, catalog_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return _editor_page(request, catalog)

    @admin.get("/catalogs/{catalog_id}/status.json")
    async def catalog_status_json(catalog_id: str):
        catalog = await run_in_threadpool(_load, catalog_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return JSONResponse(catalog["status"])

    @admin.get("/catalogs/{catalog_id}/export.yaml")
    async def export_yaml(catalog_id: str):
        catalog = await run_in_threadpool(_load, catalog_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return PlainTextResponse(
            editor.definition_yaml(catalog["definition"]),
            media_type="application/yaml",
            headers={"Content-Disposition": f'attachment; filename="{catalog_id}.yaml"'},
        )

    async def _submit(request: Request, catalog_id: str, catalog: Optional[dict[str, Any]], form: dict[str, str]):
        mode = "yaml" if form.get("mode") == "yaml" else "form"
        action = form.get("action", "validate")
        base = catalog["definition"] if catalog else {}
        values = {s.name: form.get(s.name, "") for s in editor.editor_fields()}
        yaml_text = form.get("yaml", "")
        keep = dict(values=values) if mode == "form" else dict(yaml_text=yaml_text)
        try:
            if catalog is None and not CATALOG_ID_RE.fullmatch(catalog_id):
                raise DefinitionError("catalog id: lowercase letters and digits joined by '_' (e.g. my_list)")
            raw = editor.parse_definition_yaml(yaml_text) if mode == "yaml" else editor.definition_from_form(form, base)
            checked = await editor.check_definition(
                catalog_id, raw, tmdb_client_factory=request.app.state.tmdb_client_factory
            )
            if action == "save":
                await run_in_threadpool(
                    editor.save_checked, catalog_id, checked, base_rev=catalog["config_rev"] if catalog else None
                )
                return redirect(f"/catalogs/{catalog_id}", "saved" if catalog else "created")
        except DefinitionError as e:
            return _editor_page(
                request, catalog, catalog_id=catalog_id, mode=mode, errors=str(e).split("; "), status_code=422, **keep
            )
        if mode == "form":
            keep = dict(values=values)
        return _editor_page(
            request,
            catalog,
            catalog_id=catalog_id,
            mode=mode,
            checked=checked,
            message="Valid. Not saved yet.",
            **keep,
        )

    @admin.post("/new")
    async def create(request: Request):
        form = await verified_form(request, request.state.session["sid"])
        catalog_id = form.get("catalog_id", "").strip()
        return await _submit(request, catalog_id, None, form)

    @admin.post("/catalogs/{catalog_id}/save")
    async def save(request: Request, catalog_id: str):
        form = await verified_form(request, request.state.session["sid"])
        catalog = await run_in_threadpool(_load, catalog_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if form.get("base_rev") != catalog["config_rev"]:
            return _editor_page(
                request,
                catalog,
                errors=["The catalog was changed elsewhere since this page was loaded. Your edit was not saved."],
                status_code=409,
            )
        if catalog["source"] != "editor":
            return _editor_page(
                request,
                catalog,
                errors=["This catalog is managed by its file; move it to the editor first."],
                status_code=409,
            )
        return await _submit(request, catalog_id, catalog, form)

    @admin.post("/catalogs/{catalog_id}/move-to-editor")
    async def move(request: Request, catalog_id: str):
        await verified_form(request, request.state.session["sid"])
        try:
            await run_in_threadpool(editor.move_to_editor, catalog_id)
        except DefinitionError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        return redirect(f"/catalogs/{catalog_id}", "moved")

    @admin.post("/catalogs/{catalog_id}/state")
    async def change_state(request: Request, catalog_id: str):
        form = await verified_form(request, request.state.session["sid"])
        state = form.get("state", "")
        try:
            await run_in_threadpool(editor.set_state, catalog_id, state, base_rev=form.get("base_rev", ""))
        except DefinitionError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        if state == "removed":
            return redirect("/", "deleted")
        return redirect(f"/catalogs/{catalog_id}", state)

    @admin.post("/catalogs/{catalog_id}/dry-run")
    async def dry_run(request: Request, catalog_id: str):
        await verified_form(request, request.state.session["sid"])
        code, body = await run_in_threadpool(enqueue, catalog_id, "dry_run", "admin")
        msg = {202: "dry_run_queued", 409: "already_open", 429: "daily_limit"}.get(code, "not_enabled")
        return redirect(f"/catalogs/{catalog_id}", msg)

    @admin.get("/catalogs/{catalog_id}/rollback/{snapshot_id}")
    async def confirm_rollback(request: Request, catalog_id: str, snapshot_id: int):
        catalog = await run_in_threadpool(_load, catalog_id)
        if catalog is None:
            raise HTTPException(status_code=404, detail="Not Found")
        snap = next((s for s in catalog["snapshots"] if s["id"] == snapshot_id), None)
        if snap is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return page(request, "rollback.html", catalog=catalog, snap=snap, error=None)

    @admin.post("/catalogs/{catalog_id}/rollback/{snapshot_id}")
    async def do_rollback(request: Request, catalog_id: str, snapshot_id: int):
        await verified_form(request, request.state.session["sid"])
        try:
            await run_in_threadpool(_rollback, catalog_id, snapshot_id)
        except MaintenanceError as e:
            catalog = await run_in_threadpool(_load, catalog_id)
            if catalog is None:
                raise HTTPException(status_code=404, detail="Not Found") from None
            snap = next((s for s in catalog["snapshots"] if s["id"] == snapshot_id), {"id": snapshot_id})
            return page(request, "rollback.html", 409, catalog=catalog, snap=snap, error=str(e))
        return redirect(f"/catalogs/{catalog_id}", "rolled_back")

    return admin
