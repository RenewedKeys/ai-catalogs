"""
LLM layer for curated catalogs (PLAN.md section 7).

``CuratedLLMService`` uses an ``openai.AsyncOpenAI`` client against ``OPENAI_BASE_URL``
(OpenRouter by default), strict structured output with fallbacks, and accuracy rules in the
system message, shaped for a scheduled, budgeted pipeline:

- **Every attempt is visible to the budget.** The SDK's own retries are off
  (``max_retries=0``); each attempt, including format downgrades and transient retries
  (at most 2 per call, only for timeout / 429 / 5xx / transport errors), first passes
  ``Budget.reserve()``.
- **Downgrade only on identified capability errors.** Strict ``json_schema`` ->
  ``json_object`` -> tolerant text extraction, only when the provider answered HTTP
  400/404/422 with a body naming ``response_format`` / ``json_schema`` / structured output /
  "not supported", or OpenRouter's no-endpoint-supports-the-parameters error. Any other
  4xx fails. The mode that worked is remembered per (base URL, model) for 24 hours in
  SQLite (``llm_modes``), shared by worker, CLI and dry runs.
- **Errors are raised, never swallowed.** ``LLMError`` carries a controlled ``category``
  and the HTTP status only. Provider message text is read only to classify an error and
  is never logged, stored or put into an exception message (it can quote keys).
- **Rows are validated one at a time** with Pydantic; invalid rows are dropped and counted.
  Titles are control-character-stripped and length-capped (``clean_title``) before they
  reach TMDB queries or later prompts.
- Empty content with ``finish_reason == "length"`` raises ``output_truncated`` with a
  "raise max_tokens" hint and the reasoning-token count when the provider reports it.

Per-catalog settings (``LLMSettings``): ``model``, ``temperature`` (0..2, not sent when
unset), ``max_tokens`` (default 8000, ceiling 16000), ``timeout_s`` (default 120).
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping
from urllib.parse import urlsplit

import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import settings
from app.core.logging import describe_error
from app.curation.budget import Budget
from app.curation.tmdb_gate import clean_title
from app.store import db

logger = logging.getLogger(__name__)

MAX_TOKENS_DEFAULT = 8000
MAX_TOKENS_CEILING = 16000
TIMEOUT_DEFAULT_S = 120.0
TRANSIENT_RETRIES = 2
RETRY_BACKOFF_S = (2.0, 6.0)
MODE_TTL_MS = 24 * 3600 * 1000
MAX_CATEGORY_LENGTH = 60

MODES = ("json_schema", "json_object", "text")

MOVIE_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "movies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": "integer"},
                    "category": {"type": "string"},
                },
                "required": ["title", "year", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["movies"],
    "additionalProperties": False,
}

_CAPABILITY_MARKERS = (
    "response_format",
    "json_schema",
    "structured output",
    "structured_output",
    "structured-output",
    "not supported",
    "requested parameters",  # OpenRouter: "No endpoints found that can handle the requested parameters"
    "support the requested",
)
_CAPABILITY_STATUSES = (400, 404, 422)

SYSTEM_PROMPT = """You are a film curator. You return lists of real, existing feature films.

ACCURACY REQUIREMENTS:
- Return only real films that exist; never invent titles or years.
- "year" is the original release year as an integer and MUST be accurate; it is used to
  match each film against a database. If unsure, choose the most likely year.
- "title" is the film's best-known title WITHOUT the year.
- "category" is a short label for why the film fits (use the categories you are given,
  if any; otherwise a one- or two-word label).
- No TV series, episodes or shorts. No duplicates.
"""

# For series catalogs (PLAN.md section 29). The reply key is "series".
SERIES_SYSTEM_PROMPT = """You are a TV curator. You return lists of real, existing TV series.

ACCURACY REQUIREMENTS:
- Return only real TV series that exist; never invent titles or years.
- "year" is the year the series FIRST AIRED, as an integer, and MUST be accurate; it is used
  to match each series against a database. If unsure, choose the most likely year.
- "title" is the series' best-known title WITHOUT the year or a season number.
- "category" is a short label for why the series fits (use the categories you are given,
  if any; otherwise a one- or two-word label).
- One entry per series: no individual seasons or episodes, no films, no duplicates.
"""


def list_schema(key: str) -> dict[str, Any]:
    """The strict output schema with the list under ``key`` (``movies`` or ``series``)."""
    return {
        "type": "object",
        "properties": {key: MOVIE_LIST_SCHEMA["properties"]["movies"]},
        "required": [key],
        "additionalProperties": False,
    }


class LLMError(Exception):
    """
    A model call that failed for good. ``category`` is a controlled value
    (``provider_auth``, ``provider_payment``, ``provider_rate_limit``, ``provider_http_5xx``,
    ``provider_http_4xx``, ``timeout``, ``transport``, ``output_truncated``, ``output_invalid``,
    ``config``); ``status_code`` is the HTTP status if there was one. ``hint`` is our own
    advice text. The message never contains provider text.
    """

    TRANSIENT = frozenset({"provider_rate_limit", "provider_http_5xx", "timeout", "transport"})

    def __init__(self, category: str, status_code: int | None = None, hint: str = ""):
        self.category = category
        self.status_code = status_code
        self.hint = hint
        text = category if status_code is None else f"{category} (HTTP {status_code})"
        super().__init__(f"{text}: {hint}" if hint else text)

    @property
    def transient(self) -> bool:
        return self.category in self.TRANSIENT


class _CapabilityError(Exception):
    """Internal: the provider rejected the requested output mode."""

    def __init__(self, status_code: int):
        self.status_code = status_code


# --------------------------------------------------------------------------- settings


class LLMSettings(BaseModel):
    """The per-catalog model settings of a definition."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    model: str = Field(default_factory=lambda: settings.DEFAULT_MODEL, min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int = Field(default=MAX_TOKENS_DEFAULT, ge=1, le=MAX_TOKENS_CEILING)
    timeout_s: float = Field(default=TIMEOUT_DEFAULT_S, gt=0, le=600)
    media_type: Literal["movie", "series"] = "movie"  # the definition's ``type``: films or TV series

    @classmethod
    def from_definition(cls, definition: Mapping[str, Any]) -> "LLMSettings":
        data = {k: definition[k] for k in ("model", "temperature", "max_tokens", "timeout_s") if k in definition}
        if definition.get("type") is not None:
            data["media_type"] = definition["type"]
        return cls.model_validate({k: v for k, v in data.items() if v is not None})


class Suggestion(BaseModel):
    """One validated row of the model's output."""

    model_config = ConfigDict(extra="ignore")

    title: str
    year: int = Field(ge=1870, le=2100)
    category: str = ""

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        if not isinstance(v, str):
            raise ValueError("title must be a string")
        v = clean_title(v)
        if not v:
            raise ValueError("title is empty")
        return v

    @field_validator("year", mode="before")
    @classmethod
    def _year(cls, v):
        if isinstance(v, bool):
            raise ValueError("year must be a number")
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("category must be a string")
        return clean_title(v)[:MAX_CATEGORY_LENGTH]


@dataclass
class GenerationResult:
    movies: list[Suggestion]
    dropped_rows: int
    mode: str
    attempts: int
    tokens_in: int
    tokens_out: int
    finish_reason: str | None = None
    reasoning_tokens: int | None = None


# --------------------------------------------------------------------------- mode memory


class ModeCache:
    """``llm_modes`` rows. Synchronous; called through ``asyncio.to_thread``."""

    def __init__(self, path: str | None = None):
        self.path = path

    def get(self, key: str) -> str | None:
        with db.session(self.path) as conn:
            row = conn.execute("SELECT mode, at FROM llm_modes WHERE key = ?", (key,)).fetchone()
        if row is None or db.now_ms() - row["at"] >= MODE_TTL_MS:
            return None
        return row["mode"]

    def put(self, key: str, mode: str) -> None:
        with db.session(self.path) as conn, db.transaction(conn):
            conn.execute(
                "INSERT INTO llm_modes (key, mode, at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET mode = excluded.mode, at = excluded.at",
                (key, mode, db.now_ms()),
            )


# --------------------------------------------------------------------------- parsing


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{[^{}]*\}")


def _rows_from_document(doc: Any) -> list[Any] | None:
    if isinstance(doc, dict):
        rows = doc.get("movies") if isinstance(doc.get("movies"), list) else doc.get("series")
        if isinstance(rows, list):
            return rows
        for value in doc.values():  # a different top-level key, e.g. {"films": [...]}
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        return None
    if isinstance(doc, list):
        return doc
    return None


def extract_rows(content: str, tolerant: bool) -> list[Any] | None:
    """
    The list of raw rows in a model reply. Strict: the reply is one JSON document.
    Tolerant (also used to salvage a reply cut off mid-list): code fences stripped, the
    outermost {...} or [...] tried, then every flat {...} object that parses.
    """
    text = content.strip()
    try:
        rows = _rows_from_document(json.loads(text))
        if rows is not None or not tolerant:
            return rows
    except json.JSONDecodeError:
        if not tolerant:
            return None
    text = _FENCE_RE.sub("", text).strip()
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if 0 <= start < end:
            try:
                rows = _rows_from_document(json.loads(text[start : end + 1]))
                if rows is not None:
                    return rows
            except json.JSONDecodeError:
                pass
    objects = []
    for m in _OBJECT_RE.finditer(text):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "title" in obj:
            objects.append(obj)
    return objects or None


def validate_rows(rows: list[Any]) -> tuple[list[Suggestion], int]:
    """Per-row validation: invalid rows are dropped and counted, never fatal."""
    movies, dropped = [], 0
    for row in rows:
        try:
            movies.append(Suggestion.model_validate(row))
        except ValidationError:
            dropped += 1
    return movies, dropped


# --------------------------------------------------------------------------- errors


def _error_body_text(error: openai.APIStatusError) -> str:
    """Lower-cased body text, used ONLY to classify; never logged or stored."""
    parts = []
    body = getattr(error, "body", None)
    if body is not None:
        try:
            parts.append(json.dumps(body))
        except (TypeError, ValueError):
            parts.append(repr(body))
    try:
        parts.append(error.response.text)
    except Exception:
        pass
    return " ".join(parts).casefold()


def is_capability_error(error: BaseException) -> bool:
    if not isinstance(error, openai.APIStatusError) or error.status_code not in _CAPABILITY_STATUSES:
        return False
    text = _error_body_text(error)
    return any(marker in text for marker in _CAPABILITY_MARKERS)


def _key_kind(key: str | None) -> str:
    if not key:
        return "no key is set"
    if key.startswith("sk-or-"):
        return "the key looks like an OpenRouter key"
    if key.startswith("AIza"):
        return "the key looks like a Google AI Studio (Gemini) key"
    if key.startswith("sk-"):
        return "the key looks like an OpenAI-style key"
    return "the key has an unrecognised format"


def _auth_hint() -> str:
    host = urlsplit(settings.OPENAI_BASE_URL or "").hostname or "unknown host"
    return (
        f"provider rejected the credentials; check OPENAI_API_KEY ({_key_kind(settings.OPENAI_API_KEY)}) "
        f"matches the provider at OPENAI_BASE_URL (host {host})"
    )


def classify(error: BaseException) -> LLMError:
    """Map an SDK exception to an ``LLMError`` without copying any provider text."""
    if isinstance(error, openai.APITimeoutError):
        return LLMError("timeout")
    if isinstance(error, openai.APIConnectionError):
        return LLMError("transport")
    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        if status in (401, 403):
            return LLMError("provider_auth", status, _auth_hint())
        if status == 402:
            return LLMError("provider_payment", status, "provider reports insufficient credit")
        if status == 429:
            return LLMError("provider_rate_limit", status)
        if status >= 500:
            return LLMError("provider_http_5xx", status)
        return LLMError("provider_http_4xx", status, _parameter_hint(error))
    return LLMError("transport")


def _parameter_hint(error: openai.APIStatusError) -> str:
    """Our own hint when a 4xx names a sampling parameter (no provider text is copied)."""
    text = _error_body_text(error)
    named = [p for p in ("temperature", "max_tokens") if p in text]
    if named:
        return f"provider rejected the request; it mentions {' and '.join(named)}: check the catalog's setting"
    return ""


# --------------------------------------------------------------------------- service


@dataclass
class _Reply:
    content: str
    finish_reason: str | None
    tokens_in: int
    tokens_out: int
    reasoning_tokens: int | None


def _usage(response: Any) -> tuple[int, int, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, None
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    return int(usage.prompt_tokens or 0), int(usage.completion_tokens or 0), reasoning


@dataclass
class CuratedLLMService:
    """
    One instance per run (or probe)::

        llm = CuratedLLMService(LLMSettings.from_definition(defn), budget)
        result = await llm.generate(rendered.text, request_count=60, kind="generate")

    ``http_client`` (an ``httpx2.AsyncClient``, the openai SDK's HTTP library) and ``sleep``
    are injectable for tests.
    """

    llm_settings: LLMSettings
    budget: Budget
    http_client: Any = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    use_mode_cache: bool = True
    db_path: str | None = None
    _client: openai.AsyncOpenAI | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.modes = ModeCache(self.db_path)

    @property
    def model(self) -> str:
        return self.llm_settings.model

    @property
    def mode_key(self) -> str:
        # Hashed: a base URL can embed credentials, and nothing secret goes into SQLite.
        return hashlib.sha256(f"{settings.OPENAI_BASE_URL}|{self.model}".encode("utf-8")).hexdigest()

    def client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            if not settings.OPENAI_API_KEY:
                raise LLMError("config", hint="OPENAI_API_KEY is not set")
            kwargs: dict[str, Any] = {
                "api_key": settings.OPENAI_API_KEY,
                "base_url": settings.OPENAI_BASE_URL,
                "max_retries": 0,
            }
            if self.http_client is not None:
                kwargs["http_client"] = self.http_client
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    async def aclose(self) -> None:
        # An injected http_client belongs to the caller; closing the SDK client would close it.
        if self._client is not None and self.http_client is None:
            await self._client.close()

    # ----------------------------------------------------------------- requests

    @property
    def series(self) -> bool:
        return self.llm_settings.media_type == "series"

    def _messages(self, prompt: str, request_count: int, mode: str) -> list[dict[str, str]]:
        if self.series:
            system = SERIES_SYSTEM_PROMPT + f'\nReturn exactly {request_count} TV series as JSON: {{"series": [...]}}.'
        else:
            system = SYSTEM_PROMPT + f'\nReturn exactly {request_count} films as JSON: {{"movies": [...]}}.'
        if mode != "json_schema":
            key = "series" if self.series else "movies"
            system += (
                "\nRespond with ONLY a JSON object, no prose and no code fences, of this shape: "
                f'{{"{key}": [{{"title": "string", "year": 2000, "category": "string"}}]}}'
            )
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    def _params(self, prompt: str, request_count: int, mode: str) -> dict[str, Any]:
        s = self.llm_settings
        params: dict[str, Any] = {
            "model": s.model,
            "messages": self._messages(prompt, request_count, mode),
            "max_tokens": s.max_tokens,
            "timeout": s.timeout_s,
        }
        if s.temperature is not None:
            params["temperature"] = s.temperature
        if mode == "json_schema":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": (
                    {"name": "series_list", "strict": True, "schema": list_schema("series")}
                    if self.series
                    else {"name": "movie_list", "strict": True, "schema": MOVIE_LIST_SCHEMA}
                ),
            }
        elif mode == "json_object":
            params["response_format"] = {"type": "json_object"}
        return params

    async def _attempt(self, params: dict[str, Any], kind: str) -> _Reply:
        """One budgeted outbound request. Raises ``_CapabilityError`` or ``LLMError``."""
        reservation = await self.budget.reserve(kind, self.model)
        try:
            response = await self.client().chat.completions.create(**params)
        except openai.APIError as e:
            await reservation.finish(False)
            logger.warning("[%s] model attempt failed: %s", self.model, describe_error(e))
            if is_capability_error(e):
                raise _CapabilityError(e.status_code) from None
            raise classify(e) from None
        except BaseException:
            await reservation.finish(False)
            raise
        tokens_in, tokens_out, reasoning = _usage(response)
        await reservation.finish(True, tokens_in, tokens_out)
        choice = response.choices[0] if response.choices else None
        content = (choice.message.content if choice and choice.message else None) or ""
        return _Reply(content, getattr(choice, "finish_reason", None), tokens_in, tokens_out, reasoning)

    async def _call(self, params: dict[str, Any], kind: str, counter: list[int]) -> _Reply:
        """``_attempt`` with at most 2 transient retries, each through the budget gate."""
        for retry in range(TRANSIENT_RETRIES + 1):
            counter[0] += 1
            try:
                return await self._attempt(params, kind if retry == 0 else "retry")
            except LLMError as e:
                if not e.transient or retry == TRANSIENT_RETRIES:
                    raise
                await self.sleep(RETRY_BACKOFF_S[min(retry, len(RETRY_BACKOFF_S) - 1)])
        raise AssertionError("unreachable")

    async def _start_mode(self) -> tuple[str, bool]:
        """The mode to try first, and whether it came from the 24 h memory."""
        if not self.use_mode_cache:
            return MODES[0], False
        cached = await asyncio.to_thread(self.modes.get, self.mode_key)
        return (cached, True) if cached in MODES else (MODES[0], False)

    async def generate(self, prompt: str, request_count: int, *, kind: str = "generate") -> GenerationResult:
        """Ask for ``request_count`` films. Raises ``LLMError`` or ``BudgetExceeded``."""
        start, remembered = await self._start_mode()
        counter = [0]
        tokens_in = tokens_out = 0
        mode_index = MODES.index(start)
        while True:
            mode = MODES[mode_index]
            params = self._params(prompt, request_count, mode)
            try:
                reply = await self._call(params, kind if mode == start else "downgrade", counter)
            except _CapabilityError as e:
                if mode_index + 1 >= len(MODES):
                    raise LLMError("provider_http_4xx", e.status_code, "provider rejected every output mode") from None
                mode_index += 1
                logger.info("[%s] output mode %s not supported; trying %s", self.model, mode, MODES[mode_index])
                continue
            break

        tokens_in += reply.tokens_in
        tokens_out += reply.tokens_out
        # Remember a newly found mode for 24 h; a remembered one is not refreshed, so strict
        # mode is tried again once the memory expires.
        if mode != start or (self.use_mode_cache and not remembered):
            await asyncio.to_thread(self.modes.put, self.mode_key, mode)

        if not reply.content.strip():
            if reply.finish_reason == "length":
                used = f"; reasoning used {reply.reasoning_tokens} tokens" if reply.reasoning_tokens else ""
                raise LLMError(
                    "output_truncated",
                    hint=f"model stopped at max_tokens ({self.llm_settings.max_tokens}) before any output{used}; "
                    "raise max_tokens or lower the request size",
                )
            raise LLMError("output_invalid", hint="model returned empty content")

        # Strict JSON first; tolerant extraction in text mode and to salvage a reply that
        # was cut off (finish_reason == "length") or wrapped in prose.
        rows = extract_rows(reply.content, tolerant=mode == "text")
        if rows is None:
            rows = extract_rows(reply.content, tolerant=True)
        if rows is None:
            raise LLMError("output_invalid", hint="model reply contained no list of titles")
        movies, dropped = validate_rows(rows)
        logger.info(
            "[%s] mode=%s attempts=%d rows=%d dropped=%d tokens_in=%d tokens_out=%d finish=%s",
            self.model,
            mode,
            counter[0],
            len(movies),
            dropped,
            tokens_in,
            tokens_out,
            reply.finish_reason,
        )
        return GenerationResult(
            movies=movies,
            dropped_rows=dropped,
            mode=mode,
            attempts=counter[0],
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            finish_reason=reply.finish_reason,
            reasoning_tokens=reply.reasoning_tokens,
        )
