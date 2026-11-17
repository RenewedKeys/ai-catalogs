"""
Application factory.

Layout:

    app (parent)                      no CORS
      /healthz, /static
      legacy web UI (/configure ...)  only when ENABLE_LEGACY is true
      /ops                            sub-app, no CORS, bearer OPS_TOKEN (404 when unset)
      /admin                          sub-app, no CORS, editor login (404 unless ENABLE_EDITOR)
      mounted last, at "":
        stremio_app                   CORS: any origin, GET only, no credentials
          curated catalog routes      /c/{token}/...
          legacy Stremio routes       only when ENABLE_LEGACY is true

The Stremio sub-app is a catch-all: anything registered on the parent after create_app()
returns is shadowed by it. Register /ops and /admin (each its own sub-app, without CORS)
inside create_app(), before the final mount.

Only the routes Stremio clients and AIOMetadata fetch cross-origin get CORS headers. Anything
registered on the parent, and /ops (mounted before the catch-all), is never readable from
another origin; the same holds for /admin.
"""

import re
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import Scope, Receive, Send

from app import __version__
from app.core.config import settings
from app.core.logging import describe_error, logger

# Path segments that carry a credential: /config/{encrypted blob}/... and /c/{access token}/...
_SECRET_SEGMENT = re.compile(r"/(config|c)/[^/?#]+")


def mask_url(url: str) -> str:
    """Hide credential-bearing path segments (and the query string) before logging a URL."""
    url = url.split("?", 1)[0]
    return _SECRET_SEGMENT.sub(lambda m: f"/{m.group(1)}/****", url)


class CachedStaticFiles(StaticFiles):
    """StaticFiles with browser caching headers."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add Cache-Control headers to static file responses."""

        async def send_with_cache_headers(message):
            if message["type"] == "http.response.start":
                # Cache static assets for 30 days (2592000 seconds)
                headers = list(message.get("headers", []))
                headers.append((b"Cache-Control", b"public, max-age=2592000, immutable"))
                message["headers"] = headers
            await send(message)

        await super().__call__(scope, receive, send_with_cache_headers)


def _open_store() -> None:
    from app.store.db import session

    try:
        with session():
            pass
    except Exception as e:
        logger.error(f"Cannot open the database at {settings.DATABASE_PATH}: {describe_error(e)}")
        raise


def create_app(enable_legacy: bool | None = None) -> FastAPI:
    """
    Build the ASGI application.

    Args:
        enable_legacy: mount the upstream configure UI and /config/{blob}/ routes.
            Defaults to settings.ENABLE_LEGACY.
    """
    if enable_legacy is None:
        enable_legacy = settings.ENABLE_LEGACY

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Open (and if needed create and migrate) the database now, so a missing or
        # unwritable volume stops the web service at startup instead of at the first request.
        await run_in_threadpool(_open_store)
        if enable_legacy:
            from app.services.cache import CACHE_INSTANCE

            logger.info("Legacy routes enabled. Cache backend: %s", "Redis" if CACHE_INSTANCE.is_redis else "in-memory")
        else:
            logger.info("Legacy routes disabled")
        yield

    app = FastAPI(
        title=settings.APP_NAME,
        description=settings.APP_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(GZipMiddleware, compresslevel=6)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """Log requests with credential-bearing path segments masked."""
        start_time = datetime.now()
        logger.info(f"{request.method} {mask_url(str(request.url))}")
        response = await call_next(request)
        duration = datetime.now() - start_time
        logger.info(f"Response: {response.status_code} - Duration: {duration.total_seconds()}s")
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    app.mount("/static", CachedStaticFiles(directory="./.assets"), name="static")

    # Routes fetched cross-origin by Stremio clients and AIOMetadata.
    stremio_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    stremio_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
    )
    # One override table for both apps, so tests can keep using app.dependency_overrides.
    stremio_app.dependency_overrides = app.dependency_overrides

    from app.api.curated import router as curated_router

    stremio_app.include_router(curated_router)

    if enable_legacy:
        # Imported lazily: these modules need ENCRYPTION_KEY, which is optional when legacy is off.
        from app.api.stremio import router as stremio_router
        from app.api.web import router as web_router

        app.include_router(web_router)
        stremio_app.include_router(stremio_router)

    # Operations: its own sub-app without CORS, mounted before the catch-all so its unknown
    # paths and preflights never reach the Stremio CORS middleware.
    from app.api.ops import create_ops_app

    app.mount("/ops", create_ops_app())

    # Browser editor: same pattern. With ENABLE_EDITOR off it answers 404 for every path; with it
    # on, missing or weak secrets stop the service here, at startup.
    from app.api.admin import check_editor_settings, create_admin_app

    check_editor_settings()

    @app.api_route("/admin", methods=["GET", "HEAD", "POST", "OPTIONS"], include_in_schema=False)
    async def admin_root(request: Request):
        # The mount below only matches "/admin/..."; without this the bare path would reach the
        # Stremio catch-all (and its CORS header).
        from fastapi.responses import JSONResponse, RedirectResponse

        if not settings.ENABLE_EDITOR:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return RedirectResponse("/admin/", status_code=308)

    admin_app = create_admin_app()
    app.mount("/admin", admin_app)
    app.state.admin_app = admin_app

    # Mounted last so every parent route above takes precedence.
    app.mount("", stremio_app)
    app.state.stremio_app = stremio_app
    return app


app = create_app()
