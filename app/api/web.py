"""
Web UI routes for the Stremio AI Companion application.
"""

import json
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from starlette.templating import Jinja2Templates

from app.core.config import settings
from app.core.logging import describe_error, logger
from app.models.config import Config
from app.models.enums import LLMProvider, Languages
from app.services import CATALOG_PROMPTS
from app.services.encryption import encryption_service
from app.services.validation import ConfigValidationService


def _catalog_prompts_serializable():
    out = {}
    for k, v in CATALOG_PROMPTS.items():
        out[k] = {
            "title": v.get("title"),
            "prompt": v.get("prompt"),
            "cache_ttl": v.get("cache_ttl")() if callable(v.get("cache_ttl")) else v.get("cache_ttl"),
        }
    return out


def get_request_scheme(request: Request) -> str:
    """
    Determine the correct scheme (http or https) based on request headers.

    This handles cases where the application is behind a proxy (like in Docker)
    that might forward requests internally using HTTP even if the original request
    was HTTPS.

    Args:
        request: The FastAPI request object

    Returns:
        The correct scheme (http or https)
    """
    # Check X-Forwarded-Proto header (common in proxy setups)
    forwarded_proto = request.headers.get("X-Forwarded-Proto")
    if forwarded_proto:
        return forwarded_proto

    # Check Forwarded header (RFC 7239)
    forwarded = request.headers.get("Forwarded")
    if forwarded:
        for part in forwarded.split(";"):
            if part.strip().startswith("proto="):
                return part.strip()[6:]

    # Fall back to the request's scheme
    return request.url.scheme


router = APIRouter(tags=["Web UI"])
templates = Jinja2Templates(directory="templates")

# Config fields that hold credentials. They are never sent to a template.
SECRET_CONFIG_FIELDS = {"openai_api_key", "tmdb_read_access_token", "posterdb_api_key"}


def public_settings() -> SimpleNamespace:
    """
    The only settings a template may see.

    Templates used to receive the whole settings object, which let the unauthenticated
    /configure page print the server's API keys into the HTML. Add a field here only if it
    is safe to show to anyone who can reach the server.
    """
    return SimpleNamespace(
        APP_NAME=settings.APP_NAME,
        APP_VERSION=settings.APP_VERSION,
        FOOTER_ENABLED=settings.FOOTER_ENABLED,
        OPENAI_BASE_URL=settings.OPENAI_BASE_URL,
        DEFAULT_MODEL=settings.DEFAULT_MODEL,
        PREFERRED_USER_SEARCH_LANGUAGE=settings.PREFERRED_USER_SEARCH_LANGUAGE,
        MAX_CATALOG_RESULTS=settings.MAX_CATALOG_RESULTS,
    )


def _same_base_url(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


def _scrub(text: str, secrets: list[Optional[str]]) -> str:
    """Remove credentials from a message before it is returned to the client."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "****")
    return text


@router.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    """
    Render the homepage with application description and configuration options.
    """
    return templates.TemplateResponse(request, "index.html", {"settings": public_settings()})


@router.get("/configure", response_class=HTMLResponse)
async def configure_page(request: Request, config: Optional[str] = Query(None), adult: Optional[int] = Query(None)):
    """
    Render the configuration page.

    If a config parameter is provided, it will pre-fill the form with existing settings.
    If an adult parameter is provided, it will set the adult content checkbox.
    """
    existing_config = None
    if config:
        try:
            config_data = encryption_service.decrypt(config)
            existing_config = Config.model_validate(json.loads(config_data))
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Invalid config provided to configure page: {describe_error(e)}")
            raise HTTPException(status_code=400, detail="Invalid config data")

    # Credentials are never rendered, neither the server's nor the ones inside an existing
    # config. The form only learns whether a stored value exists; leaving a field blank
    # keeps it (see save_config).
    if existing_config:
        stored = {
            "openai": bool(existing_config.openai_api_key),
            "tmdb": bool(existing_config.tmdb_read_access_token),
            "rpdb": bool(existing_config.posterdb_api_key),
        }
        existing_view = SimpleNamespace(**existing_config.model_dump(exclude=SECRET_CONFIG_FIELDS))
    else:
        stored = {
            "openai": bool(settings.OPENAI_API_KEY),
            "tmdb": bool(settings.TMDB_READ_ACCESS_TOKEN),
            "rpdb": False,
        }
        existing_view = None

    return templates.TemplateResponse(
        request,
        "configure.html",
        {
            "existing_config": existing_view,
            "existing_config_token": config if existing_config else "",
            "stored": stored,
            "settings": public_settings(),
            "LLMProvider": LLMProvider,
            "Languages": Languages,
            "CATALOG_PROMPTS": _catalog_prompts_serializable(),
        },
    )


@router.get("/config/{config}/preview", response_class=HTMLResponse)
async def preview_page(request: Request, config: str):
    """
    Render the preview page with configuration details and manifest URL.
    """
    try:
        config_data = encryption_service.decrypt(config)
        config_obj = Config.model_validate(json.loads(config_data))
        scheme = get_request_scheme(request)

        # Build manifest URLs for all types
        base_url = f"{scheme}://{request.url.netloc}/config/{config}"
        manifest_urls = {
            "combined": f"{base_url}/manifest.json",
            "movie": f"{base_url}/movie/manifest.json",
            "series": f"{base_url}/series/manifest.json",
        }
        return templates.TemplateResponse(
            request,
            "preview.html",
            {
                "manifest_url": manifest_urls["combined"],  # Keep backward compatibility
                "manifest_urls": manifest_urls,
                "config": SimpleNamespace(**config_obj.model_dump(exclude=SECRET_CONFIG_FIELDS)),
                "settings": public_settings(),
            },
        )
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in preview: {describe_error(e)}")
        raise HTTPException(status_code=400, detail="Invalid config format")
    except ValidationError as e:
        logger.error(f"Validation error in preview: {describe_error(e)}")
        raise HTTPException(status_code=400, detail="Invalid config data")
    except Exception as e:
        logger.error(f"General error in preview: {describe_error(e)}")
        raise HTTPException(status_code=400, detail="Invalid config")


@router.post("/save-config")
async def save_config(
    request: Request,
    openai_api_key: str = Form(""),
    openai_base_url: str = Form(settings.OPENAI_BASE_URL),
    model_name: str = Form(settings.DEFAULT_MODEL),
    language: str = Form(settings.PREFERRED_USER_SEARCH_LANGUAGE),
    tmdb_read_access_token: str = Form(""),
    max_results: int = Form(20),
    use_posterdb: Optional[str] = Form(None),
    posterdb_api_key: str = Form(""),
    include_catalogs_movies: Optional[list[str]] = Form(None),
    include_catalogs_series: Optional[list[str]] = Form(None),
    changed_catalogs: Optional[str] = Form("false"),
    existing_config: Optional[str] = Form(None),
):
    """
    Save configuration settings and return manifest URL.

    Handles form submission from the configure page, validates the settings,
    and returns URLs for the manifest and preview page.

    Credential fields may be left blank to keep a stored value: the one inside the config
    being edited (``existing_config``), otherwise the server's environment value. A stored
    LLM key is only reused with the base URL it was stored for, because this endpoint sends
    the key to whatever base URL the form names. The server's RPDB key is never used as a
    fallback: RPDB poster URLs contain the key and are visible to every client.
    """
    previous: Optional[Config] = None
    if existing_config:
        try:
            previous = Config.model_validate(json.loads(encryption_service.decrypt(existing_config)))
        except Exception:
            return JSONResponse({"success": False, "detail": "Invalid existing configuration"}, status_code=400)

    openai_api_key = openai_api_key.strip()
    tmdb_read_access_token = tmdb_read_access_token.strip()
    posterdb_api_key = posterdb_api_key.strip()

    if not openai_api_key:
        if previous:
            stored_key, stored_url = previous.openai_api_key, previous.openai_base_url
        else:
            stored_key, stored_url = settings.OPENAI_API_KEY, settings.OPENAI_BASE_URL
        if stored_key and _same_base_url(openai_base_url, stored_url):
            openai_api_key = stored_key
        else:
            detail = (
                "API key required: a stored key is only reused with the provider URL it was saved for"
                if stored_key
                else "API key required"
            )
            return JSONResponse({"success": False, "detail": detail}, status_code=400)

    if not tmdb_read_access_token:
        tmdb_read_access_token = (
            previous.tmdb_read_access_token if previous else (settings.TMDB_READ_ACCESS_TOKEN or "")
        )

    if not posterdb_api_key and previous and previous.posterdb_api_key:
        posterdb_api_key = previous.posterdb_api_key

    secrets = [openai_api_key, tmdb_read_access_token, posterdb_api_key]

    try:
        # Handle checkboxes: if not present in form data, it means False
        use_posterdb_bool = use_posterdb == "on" if use_posterdb else False

        # If UI sent an empty model name, fall back to environment default
        if not model_name or not model_name.strip():
            model_name = settings.DEFAULT_MODEL

        config = Config(
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            model_name=model_name,
            language=language,
            tmdb_read_access_token=tmdb_read_access_token,
            max_results=max_results,
            use_posterdb=use_posterdb_bool,
            posterdb_api_key=posterdb_api_key if posterdb_api_key else None,
            include_catalogs_movies=include_catalogs_movies,
            include_catalogs_series=include_catalogs_series,
            changed_catalogs=(changed_catalogs == "true"),
        )

        # Validate configuration by testing connections
        validation_service = ConfigValidationService()
        validation_errors = await validation_service.validate_config(config)

        if validation_errors:
            error_message = _scrub(validation_service.format_validation_errors(validation_errors), secrets)
            return JSONResponse({"success": False, "detail": error_message}, status_code=400)

        encrypted_config = encryption_service.encrypt(config.model_dump_json())
        scheme = get_request_scheme(request)

        # Build manifest URLs for all types
        base_url = f"{scheme}://{request.url.netloc}/config/{encrypted_config}"
        manifest_urls = {
            "combined": f"{base_url}/manifest.json",
            "movie": f"{base_url}/movie/manifest.json",
            "series": f"{base_url}/series/manifest.json",
        }
        return JSONResponse(
            {
                "success": True,
                "manifest_url": manifest_urls["combined"],
                "manifest_urls": manifest_urls,
                "preview_url": f"{base_url}/preview",
            }
        )
    except ValidationError as e:
        error_messages = []
        for error in e.errors():
            field = error["loc"][-1] if error["loc"] else "unknown"
            message = _scrub(str(error["msg"]), secrets)
            error_messages.append(f"{field}: {message}")

        return JSONResponse(
            {"success": False, "detail": "Validation failed: " + "; ".join(error_messages)}, status_code=400
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "detail": _scrub(f"Configuration error: {str(e)}", secrets)}, status_code=500
        )


@router.get("/config/{config}/{content_type}/configure")
async def reconfigure_page_redirect(config: str, content_type: str):
    """
    Redirect to configure page with existing config (content type agnostic).
    """
    return RedirectResponse(url=f"/configure?config={config}")


@router.get("/config/{config}")
async def config_redirect(config: str):
    """
    Redirect to configure page with existing config.
    """
    return RedirectResponse(url=f"/configure?config={config}")
