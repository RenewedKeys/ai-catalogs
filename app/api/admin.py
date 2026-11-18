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
without it is refused (403), so the editor fails closed if the proxy rule is ever removed.
Traefik's forward-auth deletes a client-sent copy of each ``authResponseHeaders`` header before
copying the auth server's, so the header cannot be forged through the proxy; anything that can
reach the container without the proxy (a published port, another container on its Docker
network) can still send it. CSRF tokens are then bound to the
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from app.api.curated import PUBLIC_SEGMENT
from app.api.ops import enqueue
from app.core.config import settings
from app.curation import editor, media
from app.curation.definitions import DefinitionError
from app.curation.status import catalog_status, iso
from app.curation.tmdb_gate import TMDBClient
from app.store import db
from app.store.catalogs import CATALOG_ID_RE
from app.store.jobs import clear_runs
from app.store.maintenance import MaintenanceError, rollback

PREFIX = "/configure"
SESSION_COOKIE = "aic_admin"
PRELOGIN_COOKIE = "aic_admin_pre"
SESSION_TTL_S = 12 * 3600
PRELOGIN_TTL_S = 3600
MIN_SECRET_BYTES = 32
MIN_PASSWORD_CHARS = 12
FILE_KEY_CHARS = 64  # editor-csrf.key: 32 random bytes, hex
LOGIN_WINDOW_MS = 15 * 60 * 1000
LOGIN_MAX_PER_CLIENT = 5
LOGIN_MAX_TOTAL = 20
SNAPSHOTS_SHOWN = 20
RUNS_SHOWN = 30  # the Activity tab lists this many; the newest few are visible, the rest behind "Show older"
FIRST_SHOWN = 5

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
    "saved": "Saved.",
    "type_changed": (
        "Saved. This catalog switched between movies and shows: add the addon again with Quick Add in AIOMetadata so it "
        "uses the new type. The previous list keeps showing until the first new one is published."
    ),
    "created": "Catalog created.",
    "moved": "You can edit this catalog here now.",
    "enabled": "Catalog is visible.",
    "disabled": "Catalog is hidden.",
    "dry_run_queued": "Test run started. The results appear below in a moment.",
    "refresh_queued": "Running. The new version appears below in a moment.",
    "already_open": "This catalog is already running. Wait for it to finish.",
    "daily_limit": "This catalog has reached its runs for today. Try again tomorrow.",
    "not_enabled": "This catalog is hidden. Make it visible to run it.",
    "rolled_back": "Done. That earlier version is showing again.",
    "deleted": "Catalog deleted.",
    "runs_cleared": "Old runs cleared.",
    "nothing_to_clear": "Nothing to clear.",
    "signed_back_in": "You're signed in again. If you had just pressed a button, it didn't go through: press it again.",
}

RUN_KINDS = {"refresh": "Refresh", "dry_run": "Test run"}
RUN_STATUSES = {
    "ok": "done",
    "running": "running",
    "failed": "failed",
    "superseded": "replaced by a newer one",
    "abandoned": "interrupted",
}
REJECT_LABELS = {
    "no_match": "not found on TMDB",
    "not_found": "not found on TMDB",
    "runtime_missing": "no runtime",
    "runtime_short": "too short",
    "rating_low": "rating too low",
    "votes_low": "too few ratings",
    "language": "other language",
    "genre_excluded": "excluded genre",
    "year_missing": "no year",
    "year_out_of_range": "outside your years",
    "adult": "adult",
    "not_released": "not released yet",
    "imdb_id_missing": "no IMDb id",
    "blacklisted": "on your never-include list",
}


STAGE_LABELS = {
    "model_calls_ok": "AI requests",
    "topup_rounds": "extra rounds to fill gaps",
    "generated": "suggestions from the AI",
    "dropped_rows": "unreadable suggestions",
    "unique": "different movies",
    "duplicate_id": "duplicates",
    "eligible": "passed your rules",
    "held_for_history": "skipped because shown recently",
    "selected": "picked",
    "unmet_hard": "decade minimums missed",
    "unmet_soft": "theme minimums missed",
    "stopped_by_budget": "stopped by the daily AI limit",
    "watched_known": "watched movies known",
}


STAGE_LABELS_ONE = {
    "model_calls_ok": "AI request",
    "topup_rounds": "extra round to fill gaps",
    "dropped_rows": "unreadable suggestion",
    "unique": "different movie",
    "duplicate_id": "duplicate",
    "unmet_hard": "decade minimum missed",
    "unmet_soft": "theme minimum missed",
    "watched_known": "watched movie known",
}


def run_details(run: dict[str, Any], kind: str = media.MOVIE) -> list[str]:
    """The run's counts as short plain phrases, e.g. '2 AI requests'."""
    return [editor.say(line, kind) for line in _run_details(run)]


def _run_details(run: dict[str, Any]) -> list[str]:
    out = []
    for key, n in (run.get("stage_counts") or {}).items():
        if key in ("generated", "eligible", "selected"):
            continue  # shown as the big numbers
        if (
            key in ("dropped_rows", "duplicate_id", "topup_rounds", "held_for_history", "unmet_hard", "unmet_soft")
            and not n
        ):
            continue
        label = STAGE_LABELS_ONE.get(key) if n == 1 and key in STAGE_LABELS_ONE else None
        label = label or STAGE_LABELS.get(key, key.replace("_", " "))
        out.append("stopped by the daily AI limit" if key == "stopped_by_budget" else f"{n} {label}")
    if run.get("tokens_in") or run.get("tokens_out"):
        out.append(f"{run.get('tokens_in') or 0:,} tokens sent, {run.get('tokens_out') or 0:,} received")
    return out


_FRIENDLY = [
    (re.compile(r"Input should be a valid integer.*"), "enter a whole number"),
    (re.compile(r"Input should be a valid number.*"), "enter a number"),
    (re.compile(r"Input should be greater than or equal to (\S+)"), r"must be at least \1"),
    (re.compile(r"Input should be greater than (\S+)"), r"must be more than \1"),
    (re.compile(r"Input should be less than or equal to (\S+)"), r"must be at most \1"),
    (re.compile(r"Input should be less than (\S+)"), r"must be less than \1"),
    (re.compile(r"String should have at least 1 character.*"), "can't be empty"),
    (re.compile(r"Field required.*"), "is required"),
    (re.compile(r"invalid YAML.*"), "couldn't read this value"),
    (re.compile(r"not a number$"), "enter a number"),
    (
        re.compile(r"template uses unknown variable\(s\): (.*)"),
        r"has {{ \1 }} in it, which isn't supported. Remove the curly braces.",
    ),
]


def friendly_error(message: str, kind: str = media.MOVIE) -> str:
    """'year_min: Input should be ...' -> 'Released from: enter a whole number'; field names anywhere
    in the message become their on-page labels."""
    message = re.sub(r"^Value error, ", "", message.strip())
    message = message.replace(
        "schedule.cron must have five fields: minute hour day-of-month month day-of-week",
        "Refresh: a custom schedule needs five parts (minute hour day month weekday)",
    )
    field, sep, rest = message.partition(": ")
    if sep and re.fullmatch(r"[a-z_]+(\.[a-z0-9_]+)*", field) and field.split(".")[0] in editor.LABELS:
        for pattern, replacement in _FRIENDLY:
            if pattern.match(rest):
                rest = pattern.sub(replacement, rest, count=1)
                break
        message = f"{editor.label_of(field.split('.')[0], kind)}: {rest}"
    names = sorted(editor.LABELS, key=len, reverse=True)
    message = re.sub(
        r"\b(" + "|".join(map(re.escape, names)) + r")\b(?!\s*:)",
        lambda m: f"“{editor.label_of(m.group(1), kind)}”",
        message,
    )
    message = message.replace("publish_partial", "“Publish what was found”").replace(" films", " movies")
    message = message.replace(" film(s)", " movie(s)")
    return editor.say(message, kind)


def _form_kind(form: Mapping[str, Any], mode: str, base: Mapping[str, Any]) -> str:
    if mode == "form":
        return media.SERIES if form.get("type") == media.SERIES else media.MOVIE
    return media.media_type(base)


def ago(value: Any, now: Optional[datetime] = None) -> str:
    """'2026-11-17T23:12:55Z' -> '5 min ago' / '3 h ago' / 'yesterday' / '12 Nov'."""
    try:
        then = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return when(value)
    seconds = int(((now or datetime.now(timezone.utc)) - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    if seconds < 2 * 86400:
        return "yesterday"
    if seconds < 7 * 86400:
        return f"{seconds // 86400} days ago"
    return then.strftime("%-d %b")


def when(value: Any) -> str:
    """'2026-11-17T23:12:55Z' -> '2026-11-17 18:12 EST' in the server's TZ (UTC when unset)."""
    text = str(value or "")
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return text
    local = moment.astimezone(ZoneInfo(editor.server_timezone_name()))
    return local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or "")


# Why a run failed, in plain words, with what to do about it. Keys are the controlled
# runs.error_category values; anything else falls back to the raw code.
RUN_ERRORS = {
    "shortfall": (
        "Couldn't find enough that fit all your rules",
        "Loosen a rule (often minimum number of ratings or minimums per decade), or set "
        '"When a refresh comes up short" to "Publish what was found". The current version stays.',
    ),
    "budget": (
        "Stopped by the AI request limit",
        'The daily AI limit or this catalog\'s "Most AI requests per run" was reached. Try again later or '
        "raise the limit.",
    ),
    "config": ("A setting needs fixing", "Open Settings, press Preview and fix what it reports."),
    "assertion": ("Something went wrong inside the app", "Run it again; if it keeps happening, check the logs."),
    "timeout": ("The AI took too long to answer", 'Run it again, or raise "Give up on the AI after".'),
    "transport": ("Couldn't reach the AI provider", "Check the server's internet connection and try again."),
    "output_invalid": (
        "The AI's answer couldn't be read",
        "Try again, or pick a different AI model under Advanced settings.",
    ),
    "provider_auth": ("The AI key was refused", "Check OPENAI_API_KEY / GEMINI_API_KEY on the server."),
    "provider_payment": ("The AI account is out of credit", "Top up your OpenRouter or Google account."),
    "provider_rate_limit": ("The AI provider is busy (rate limit)", "Wait a little and try again."),
    "provider_http_5xx": ("The AI provider had an error", "Wait a little and try again."),
    "provider_http_4xx": (
        "The AI provider rejected the request",
        "Check the AI model name under Advanced settings.",
    ),
    "mdblist_auth": ("Your MDBList key was refused", "Check MDBLIST_API_KEY on the server."),
    "mdblist_rate_limit": ("MDBList is busy (rate limit)", "Wait a little and try again."),
    "mdblist_timeout": ("MDBList took too long to answer", "Try again."),
    "mdblist_transport": ("Couldn't reach MDBList", "Check the server's internet connection and try again."),
    "mdblist_schema": ("MDBList answered in an unexpected way", "Try again later."),
    "lost_lease": ("Interrupted by a restart", "Run it again."),
}


def run_error(run: dict[str, Any]) -> tuple[str, str]:
    category = run.get("error_category") or ""
    if category in RUN_ERRORS:
        return RUN_ERRORS[category]
    return (category.replace("_", " ").capitalize() or "Failed", "")


def run_kind(run: dict[str, Any]) -> str:
    return RUN_KINDS.get(run.get("kind"), str(run.get("kind", "")).replace("_", " ").capitalize())


def run_status(run: dict[str, Any]) -> str:
    return RUN_STATUSES.get(run.get("status"), str(run.get("status", "")))


def reject_label(key: str) -> str:
    return REJECT_LABELS.get(key, key.replace("_", " "))


class EditorConfigError(RuntimeError):
    """ENABLE_EDITOR is on but the editor's settings are unusable (startup refuses)."""


def proxy_mode() -> bool:
    return settings.EDITOR_AUTH == "proxy"


def check_editor_settings() -> None:
    if not settings.ENABLE_EDITOR:
        return
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
            if len(key) == FILE_KEY_CHARS:
                break
            time.sleep(0.02)
        if len(key) != FILE_KEY_CHARS:
            raise EditorConfigError(f"{path} holds no usable key; delete it to have a new one made") from None
    else:
        key = secrets.token_hex(FILE_KEY_CHARS // 2).encode()
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


def browser_page_load(request: Request) -> bool:
    """True for a page the browser opens in the address bar, not the editor's own background requests."""
    dest = request.headers.get("sec-fetch-dest")
    if dest:
        return dest == "document"
    return "text/html" in (request.headers.get("accept") or "")


def page_after_sign_in(path: str) -> str:
    """Where a browser goes when it lands on a URL that is not a page (see the guard)."""
    m = re.fullmatch(r"/catalogs/([^/]+)/[^/]+", path)
    return f"/catalogs/{m.group(1)}" if m and CATALOG_ID_RE.fullmatch(m.group(1)) else "/"


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
            "SELECT catalog_id, config_rev, definition_json FROM catalog_defs WHERE state != 'removed' "
            "ORDER BY position, catalog_id"
        ).fetchall()
        return [
            {
                **catalog_status(conn, r["catalog_id"], now),
                "config_rev": r["config_rev"],
                "kind": media.media_type(json.loads(r["definition_json"])),
            }
            for r in rows
        ]


def _reorder(ids: list[str]) -> int:
    """The dashboard's drag order: positions 0..n-1 in the given order. Definitions are untouched."""
    with db.session() as conn, db.transaction(conn):
        live = {r[0] for r in conn.execute("SELECT catalog_id FROM catalog_defs WHERE state != 'removed'")}
        ordered = [i for i in dict.fromkeys(ids) if i in live]
        rest = conn.execute(
            "SELECT catalog_id FROM catalog_defs WHERE state != 'removed' ORDER BY position, catalog_id"
        ).fetchall()
        ordered += [r[0] for r in rest if r[0] not in ordered]
        for pos, cid in enumerate(ordered):
            conn.execute("UPDATE catalog_defs SET position = ? WHERE catalog_id = ?", (pos, cid))
    return len(ordered)


# ------------------------------------------------------------------ suggestions (pickers)

SUGGEST_KINDS = {"movie", "tv", "collection", "company", "keyword", "model"}
MAX_SUGGESTIONS = 12
_MODELS: dict[str, Any] = {"at": 0.0, "ids": []}
MODELS_TTL_S = 3600


async def _model_ids() -> list[str]:
    """Model ids the configured provider lists (OpenAI-compatible GET /models), cached an hour."""
    if _MODELS["ids"] and time.time() - _MODELS["at"] < MODELS_TTL_S:
        return _MODELS["ids"]
    import httpx

    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"} if settings.OPENAI_API_KEY else {}
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(settings.OPENAI_BASE_URL.rstrip("/") + "/models", headers=headers)
        r.raise_for_status()
        data = r.json().get("data") or []
    raw = [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]
    ids = sorted({i.removeprefix("models/") for i in raw})  # Google lists "models/<id>"
    _MODELS.update(at=time.time(), ids=ids)
    return ids


async def suggestions(kind: str, query: str, tmdb_client_factory: Callable[[], TMDBClient]) -> list[dict[str, str]]:
    query = query.strip()[:100]
    if kind not in SUGGEST_KINDS or len(query) < 2:
        return []
    if kind == "model":
        words = query.lower().split()
        ids = await _model_ids()
        return [{"label": m, "value": m} for m in ids if all(w in m.lower() for w in words)][:MAX_SUGGESTIONS]
    client = tmdb_client_factory()
    try:
        if kind in ("movie", "tv"):
            params = {"query": query, "include_adult": "false", "language": "en-US", "page": "1"}
            results = (await client.get(f"/search/{kind}", params)).get("results") or []
            name_key, date_key = ("name", "first_air_date") if kind == "tv" else ("title", "release_date")
            out = []
            for r in results:
                title, year = (r.get(name_key) or "").strip(), (r.get(date_key) or "")[:4]
                if title:
                    text = f"{title} ({year})" if year.isdigit() else title
                    out.append({"label": text, "value": text, "votes": r.get("vote_count") or 0})
            out.sort(key=lambda o: -o["votes"])
            return [{"label": o["label"], "value": o["value"]} for o in out][:MAX_SUGGESTIONS]
        results = (await client.get(f"/search/{kind}", {"query": query, "page": "1"})).get("results") or []
        names = [(r.get("name") or "").strip() for r in results]
        return [{"label": n, "value": n} for n in dict.fromkeys(n for n in names if n)][:MAX_SUGGESTIONS]
    finally:
        await client.aclose()


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
        out["status"] = catalog_status(conn, catalog_id, db.now_ms(), runs_limit=RUNS_SHOWN)
        run_ids = [r["id"] for r in out["status"]["recent_runs"]]
        previews = {}
        if run_ids:
            marks = ",".join("?" * len(run_ids))
            for r in conn.execute(f"SELECT id, preview_json FROM runs WHERE id IN ({marks})", run_ids):
                previews[r["id"]] = _picks(r["preview_json"])
        for run in out["status"]["recent_runs"]:
            run["picks"] = previews.get(run["id"])
        snaps = conn.execute(
            """
            SELECT s.id, s.created_at, s.item_count, s.status, s.metas_json IS NOT NULL AS has_payload,
                   s.metas_json, a.id AS activation_id, a.revoked_at
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
            "movies": _snapshot_movies(s["metas_json"]),
            "eligible": s["status"] != "active"
            and bool(s["has_payload"])
            and s["activation_id"] is not None
            and s["revoked_at"] is None,
        }
        for s in snaps
    ]
    return out


def _picks(text: Optional[str]) -> Optional[list[dict[str, Any]]]:
    """A test run's stored picks as {title, year, imdb_id}; None when it stored none."""
    if not text:
        return None
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, list) and r and isinstance(r[0], str):
            out.append({"title": r[0], "year": r[1] if len(r) > 1 else None, "imdb_id": r[2] if len(r) > 2 else None})
    return out


def _snapshot_movies(text: Optional[str]) -> Optional[list[dict[str, Any]]]:
    if not text:
        return None
    try:
        metas = json.loads(text)
    except ValueError:
        return None
    return [
        {"title": m.get("name") or "", "year": m.get("releaseInfo"), "imdb_id": m.get("id")}
        for m in metas
        if isinstance(m, dict)
    ]


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
    env.filters["when"] = when
    env.filters["ago"] = ago
    env.globals["first_shown"] = FIRST_SHOWN
    env.filters["say"] = editor.say
    from app import __version__

    env.globals["version"] = __version__
    env.globals.update(
        run_error=run_error,
        run_kind=run_kind,
        run_status=run_status,
        reject_label=reject_label,
        run_details=run_details,
    )

    def page(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
        sess = getattr(request.state, "session", None)
        context.setdefault("csrf", csrf_token(sess["sid"]) if sess else "")
        context.setdefault("logged_in", sess is not None)
        context["prefix"] = PREFIX
        context["proxy_auth"] = proxy_mode()
        context["user"] = sess.get("user") if sess else None
        return HTMLResponse(env.get_template(template).render(**context), status_code=status_code)

    def redirect(path: str, msg: Optional[str] = None, tab: str = "") -> RedirectResponse:
        url = PREFIX + path + (f"?msg={msg}" if msg else "") + (f"#{tab}" if tab else "")
        return RedirectResponse(url, status_code=303)

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
        elif request.method == "GET" and path.endswith("/status.json") and browser_page_load(request):
            # The status poll is a background request. When the proxy's sign-in expires it can send
            # the browser back here after signing in again: show the catalog, not raw JSON.
            response = redirect(page_after_sign_in(path), "signed_back_in", "activity-tab")
        else:
            response = await call_next(request)
            if request.method == "GET" and response.status_code == 405 and browser_page_load(request):
                # A button pressed after the sign-in expired: the proxy returns the browser to the
                # button's URL with a GET. The action did not run; go back to the page it came from.
                target = page_after_sign_in(path)
                response = redirect(target, "signed_back_in", "activity-tab" if target != "/" else "")
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
        kind = media.SERIES if (values or {}).get("type", definition.get("type")) == media.SERIES else media.MOVIE
        return page(
            request,
            "catalog.html",
            status_code,
            kind=kind,
            saved_kind=media.media_type(definition),
            movie_only=editor.MOVIE_ONLY_FIELDS,
            series_genres=editor.SERIES_GENRES,
            catalog=catalog,
            catalog_id=catalog["catalog_id"] if catalog else catalog_id,
            basic=editor.basic_fields(),
            advanced=editor.advanced_groups(),
            label_of=editor.label_of,
            help_of=editor.help_of,
            weekdays=list(enumerate(editor.WEEKDAY_NAMES)),
            mdblist_ready=bool(settings.MDBLIST_API_KEY),
            option_label=editor.option_label,
            current_json={n: json.dumps(definition.get(n)) for n in editor.WIDGETS},
            widgets=editor.WIDGETS,
            languages=editor.LANGUAGES,
            genres=editor.GENRES,
            decade_options=[[d, f"{d}s"] for d in editor.DECADES],
            values=values if values is not None else editor.form_values(definition),
            yaml_text=yaml_text if yaml_text is not None else editor.definition_yaml(definition),
            mode=mode,
            errors=errors or [],
            checked=checked,
            notice=message or notice(request),
            file_present=_file_exists(catalog["catalog_id"]) if catalog else None,
            editable=catalog is None or catalog["source"] == "editor",
        )

    @admin.post("/order")
    async def reorder(request: Request):
        form = await verified_form(request, request.state.session["sid"])
        ids = [i for i in (form.get("order") or "").split(",") if CATALOG_ID_RE.fullmatch(i)]
        count = await run_in_threadpool(_reorder, ids)
        return JSONResponse({"ok": True, "count": count})

    @admin.get("/suggest")
    async def suggest(request: Request, kind: str = "", q: str = ""):
        try:
            items = await suggestions(kind, q, request.app.state.tmdb_client_factory)
        except Exception as e:  # suggestions are a convenience: never an error page
            from app.core.logging import describe_error, logger

            logger.warning("Suggestions for %s failed: %s", kind, describe_error(e))
            items = []
        return JSONResponse(items)

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
                switched = catalog is not None and media.media_type(base) != media.media_type(checked.definition)
                return redirect(
                    f"/catalogs/{catalog_id}", "type_changed" if switched else ("saved" if catalog else "created")
                )
        except DefinitionError as e:
            return _editor_page(
                request,
                catalog,
                catalog_id=catalog_id,
                mode=mode,
                errors=[friendly_error(m, _form_kind(form, mode, base)) for m in str(e).split("; ")],
                status_code=422,
                **keep,
            )
        if mode == "form":
            keep = dict(values=values)
        return _editor_page(
            request,
            catalog,
            catalog_id=catalog_id,
            mode=mode,
            checked=checked,
            message="Looks good. Not saved yet.",
            **keep,
        )

    @admin.post("/new")
    async def create(request: Request):
        form = await verified_form(request, request.state.session["sid"])
        catalog_id = form.get("catalog_id", "").strip()
        if not catalog_id:  # the simple form has no id field: derive it from the name
            name = form.get("name", "")
            if form.get("mode") == "yaml":
                try:
                    parsed = editor.parse_definition_yaml(form.get("yaml", ""))
                    name = str(parsed.get("name") or "") if isinstance(parsed, dict) else ""
                except DefinitionError:
                    name = ""
            taken = await run_in_threadpool(editor.taken_catalog_ids)
            catalog_id = editor.catalog_id_for(name, taken)
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
        return redirect(f"/catalogs/{catalog_id}", state, "activity-tab")

    @admin.post("/catalogs/{catalog_id}/clear-runs")
    async def clear_old_runs(request: Request, catalog_id: str):
        await verified_form(request, request.state.session["sid"])
        if await run_in_threadpool(_load, catalog_id) is None:
            raise HTTPException(status_code=404, detail="Not Found")

        def _clear() -> int:
            with db.session() as conn:
                return clear_runs(conn, catalog_id)

        n = await run_in_threadpool(_clear)
        return redirect(f"/catalogs/{catalog_id}", "runs_cleared" if n else "nothing_to_clear", "activity-tab")

    @admin.post("/catalogs/{catalog_id}/dry-run")
    async def dry_run(request: Request, catalog_id: str):
        await verified_form(request, request.state.session["sid"])
        code, body = await run_in_threadpool(enqueue, catalog_id, "dry_run", "admin")
        msg = {202: "dry_run_queued", 409: "already_open", 429: "daily_limit"}.get(code, "not_enabled")
        return redirect(f"/catalogs/{catalog_id}", msg, "activity-tab")

    @admin.post("/catalogs/{catalog_id}/refresh")
    async def refresh_now(request: Request, catalog_id: str):
        await verified_form(request, request.state.session["sid"])
        code, body = await run_in_threadpool(enqueue, catalog_id, "refresh", "admin")
        msg = {202: "refresh_queued", 409: "already_open", 429: "daily_limit"}.get(code, "not_enabled")
        return redirect(f"/catalogs/{catalog_id}", msg, "activity-tab")

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
        return redirect(f"/catalogs/{catalog_id}", "rolled_back", "activity-tab")

    return admin
