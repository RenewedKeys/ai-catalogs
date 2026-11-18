"""
Curated catalog routes, read by AIOMetadata (PLAN.md section 9).

    GET /c/{token}/manifest.json
    GET /c/{token}/catalog/{type}/{catalog_id}.json          the whole active snapshot
    GET /c/{token}/catalog/{type}/{catalog_id}/{extra}.json  extra = "skip=N[&genre=G]"

``type`` is ``movie`` or ``series``: the type of the catalog's active snapshot (PLAN.md section
29). A catalog switched from movies to shows keeps serving its last movie list as movies until
its first show list is published; any other type is a 404.

Single-response contract: offset 0 returns the entire active snapshot and any ``skip > 0``
returns ``{"metas": []}``. AIOMetadata advances its upstream offset by the number of items
it received and stops on an empty page, so it always ends after one request, and an offset
can never mix items from two different snapshots. The manifest declares no ``skip`` extra.

These routes never call the LLM; they only read SQLite. They are plain ``def`` handlers, so
FastAPI runs them in its thread pool and the blocking SQLite calls never stall the loop.
"""

import hmac
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.store.catalogs import CATALOG_ID_RE, MEDIA_TYPES, active_metas, catalog_state, servable_catalogs
from app.store.db import session

router = APIRouter(tags=["Curated catalogs"])

NO_STORE = {"Cache-Control": "no-store"}
PUBLIC_SEGMENT = "public"


def _check_token(token: str) -> None:
    """Constant-time comparison; a wrong token looks exactly like a missing route."""
    expected = settings.ADDON_ACCESS_TOKEN or PUBLIC_SEGMENT
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=404, detail="Not Found")


def _manifest(catalogs) -> dict:
    manifest = {
        "id": settings.CURATED_ADDON_ID,
        "version": settings.APP_VERSION,
        "name": settings.CURATED_ADDON_NAME,
        "description": settings.APP_DESCRIPTION,
        "resources": ["catalog"],
        "types": [t for t in MEDIA_TYPES if any(c.media_type == t for c in catalogs)] or ["movie"],
        "idPrefixes": ["tt"],
        "catalogs": [{"type": c.media_type, "id": c.catalog_id, "name": c.name} for c in catalogs],
        "behaviorHints": {"configurable": False, "configurationRequired": False},
    }
    if settings.PUBLIC_BASE_URL:
        manifest["logo"] = f"{settings.PUBLIC_BASE_URL}/static/logo_256.png"
        manifest["background"] = f"{settings.PUBLIC_BASE_URL}/static/background.png"
    return manifest


@router.get("/c/{token}/manifest.json")
def curated_manifest(token: str = Path(...)):
    _check_token(token)
    with session() as conn:
        catalogs = servable_catalogs(conn)
    return JSONResponse(_manifest(catalogs), headers=NO_STORE)


def _parse_skip(extra: str) -> int:
    """Parse a Stremio extra segment such as ``skip=100`` or ``skip=100&genre=Action``."""
    params = parse_qs(extra, keep_blank_values=True, strict_parsing=False)
    unknown = set(params) - {"skip", "genre"}
    if unknown or not params:
        raise HTTPException(status_code=404, detail="Not Found")
    raw = params.get("skip", ["0"])[-1]
    if not raw.isdigit() or len(raw) > 9:
        raise HTTPException(status_code=400, detail="Invalid skip")
    return int(raw)


def _catalog_response(media_type: str, catalog_id: str, skip: int) -> JSONResponse:
    if media_type not in MEDIA_TYPES or not CATALOG_ID_RE.fullmatch(catalog_id):
        raise HTTPException(status_code=404, detail="Unknown catalog")
    with session() as conn:
        if catalog_state(conn, catalog_id) != "enabled":
            raise HTTPException(status_code=404, detail="Unknown catalog")
        metas = active_metas(conn, catalog_id)
        if metas is not None and active_metas(conn, catalog_id, media_type) is None:
            raise HTTPException(status_code=404, detail="Unknown catalog")  # served as the other type
    if metas is None:
        # Not listed in the manifest until its first snapshot exists, so this only happens when
        # it was imported earlier and the state was then lost. AIOMetadata turns any error into
        # an empty list, so this status is a signal for our own logs, not for the client.
        return JSONResponse({"metas": []}, status_code=503, headers=NO_STORE)
    if skip > 0:
        return JSONResponse({"metas": []}, headers=NO_STORE)
    return JSONResponse({"metas": metas}, headers=NO_STORE)


@router.get("/c/{token}/catalog/{media_type}/{catalog_id}.json")
def curated_catalog(token: str = Path(...), media_type: str = Path(...), catalog_id: str = Path(...)):
    _check_token(token)
    return _catalog_response(media_type, catalog_id, 0)


@router.get("/c/{token}/catalog/{media_type}/{catalog_id}/{extra}.json")
def curated_catalog_extra(
    token: str = Path(...), media_type: str = Path(...), catalog_id: str = Path(...), extra: str = Path(...)
):
    _check_token(token)
    return _catalog_response(media_type, catalog_id, _parse_skip(extra))
