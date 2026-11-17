"""
Catalog definitions: ``catalogs.d/*.yaml`` -> ``catalog_defs`` (PLAN.md sections 3 and 6).

One file per catalog; the catalog id is the file name stem (``my_list.yaml`` -> ``my_list``),
so even a file that does not parse has an id to record its error against.

``validate_dir`` checks every file and writes nothing. ``apply_dir`` is the one loader:

1. parse and validate every file (Pydantic, the limits, the model and budget settings, and
   the prompt template rendered against a sample context);
2. resolve each valid definition's exclusion names to TMDB ids automatically
   (``app.curation.exclusions``), over the network and *before* any write; only when the
   names changed since the last accepted revision. If TMDB fails, that definition is not
   accepted (a hard filter must never be silently off) and the error is recorded;
3. in **one** transaction: accept every valid definition (``config_rev`` = sha256 of the
   canonical definition; a new revision bumps the epoch), record the error of every invalid
   one while its previously accepted definition stays in force, and mark catalogs whose file
   is gone ``removed``. Only catalogs that came from a file are ever marked removed, and a
   missing directory is an error, never "every catalog removed".

Values must be plain YAML (text, numbers, booleans, lists, mappings); a date or a timestamp
is rejected, so the stored JSON equals what the file says.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from app.core.config import settings
from app.curation.budget import BudgetLimits
from app.curation.exclusions import ExclusionInputs, ResolvedExclusions, exclusion_inputs, resolve_exclusions
from app.curation.limits import limits_from_definition
from app.curation.llm import LLMSettings
from app.curation.prompt import PromptError, validate_definition_prompt
from app.curation.schedule import parse_schedule
from app.curation.tmdb_gate import TMDBClient, TMDBError
from app.store import db
from app.store.catalogs import CATALOG_ID_RE, MAX_ITEMS, accept_definition, config_rev_of

MAX_FILE_BYTES = 256 * 1024
MAX_ERROR_CHARS = 2000


class DefinitionError(ValueError):
    """A definition file that cannot be accepted. The message is our own text, safe to store."""


class LoaderError(RuntimeError):
    """The directory itself is unusable; nothing was changed."""


class CatalogDefinition(BaseModel):
    """
    Every key a definition may carry (PLAN.md section 6). Unknown keys are errors, so a typo
    never silently turns a limit off. Only ``prompt`` is required; everything else is off or
    neutral unless set.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    name: str | None = Field(default=None, max_length=100)
    enabled: bool = True
    position: int | None = None

    # Prompt variables
    focus: str | None = None
    weekday_focus: dict[str, str] = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    history_prompt_limit: int | None = Field(default=None, ge=0, le=1000)
    max_prompt_chars: int | None = Field(default=None, ge=1000, le=200_000)

    # Exclusions (resolved to TMDB ids when applied)
    blacklist_titles: list[Any] = Field(default_factory=list)
    blacklist_tmdb_ids: list[int] = Field(default_factory=list)
    franchise_denylist: Any = None

    # Limits (app.curation.limits validates them)
    min_vote_average: Any = None
    min_vote_count: Any = None
    year_min: Any = None
    year_max: Any = None
    min_runtime: Any = None
    languages: Any = None
    excluded_genre_ids: Any = None
    max_per_collection: Any = None
    director_cap: Any = None
    decade_min: Any = None
    category_min: Any = None

    # Model (app.curation.llm validates them)
    model: Any = None
    temperature: Any = None
    max_tokens: Any = None
    timeout_s: Any = None

    # Run shape
    target_count: int = Field(default=100, ge=1, le=MAX_ITEMS)
    overgenerate_factor: float = Field(default=1.6, ge=1.0, le=5.0)
    history_window_runs: int = Field(default=7, ge=0, le=1000)
    history_mode: Literal["strict", "relax"] = "strict"
    on_shortfall: Literal["fail", "publish_partial"] = "fail"
    min_accept: int | None = Field(default=None, ge=1, le=MAX_ITEMS)
    max_model_calls_per_run: Any = None
    max_runs_per_day: Any = None
    schedule: dict[str, Any] | None = None  # cron + timezone (app.curation.schedule validates it)

    @model_validator(mode="after")
    def _consistent(self):
        if self.on_shortfall == "publish_partial" and self.min_accept is None:
            raise ValueError("on_shortfall: publish_partial needs min_accept")
        if self.min_accept is not None and self.min_accept > self.target_count:
            raise ValueError("min_accept must not be greater than target_count")
        return self


def _format_validation_error(e: ValidationError) -> str:
    """Field paths and messages only; never the rejected input values."""
    parts = []
    for err in e.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _check_feasible(defn: Mapping[str, Any], target: int) -> None:
    limits = limits_from_definition(defn)
    if sum(limits.decade_min.values()) > target:
        raise DefinitionError(f"decade_min asks for {sum(limits.decade_min.values())} films, target_count is {target}")
    if sum(limits.category_min.values()) > target:
        raise DefinitionError(
            f"category_min asks for {sum(limits.category_min.values())} films, target_count is {target}"
        )
    for decade in limits.decade_min:
        if (limits.year_max is not None and decade > limits.year_max) or (
            limits.year_min is not None and decade + 9 < limits.year_min
        ):
            raise DefinitionError(f"decade_min: the {decade}s lie outside year_min/year_max")


def validate_definition(raw: Any) -> tuple[dict[str, Any], CatalogDefinition, ExclusionInputs]:
    """
    Validate one parsed definition. Returns the canonical definition (what is stored and
    hashed), the parsed model and the exclusion inputs. Raises ``DefinitionError``.
    """
    if not isinstance(raw, Mapping):
        raise DefinitionError("a definition must be a mapping of keys to values")
    try:
        # Canonical form: JSON types only (mapping keys become text), as stored in SQLite.
        canonical = json.loads(json.dumps(raw, ensure_ascii=False))
    except (TypeError, ValueError):
        raise DefinitionError(
            "values must be plain text, numbers, booleans, lists or mappings (quote dates and times)"
        ) from None
    try:
        model = CatalogDefinition.model_validate(canonical)
        limits_from_definition(canonical)
        LLMSettings.from_definition(canonical)
        BudgetLimits.from_definition(canonical)
        parse_schedule(canonical.get("schedule"))
    except ValidationError as e:
        raise DefinitionError(_format_validation_error(e)) from None
    except ValueError as e:
        raise DefinitionError(str(e)) from None
    _check_feasible(canonical, model.target_count)
    try:
        inputs = exclusion_inputs(canonical)
    except ValueError as e:
        raise DefinitionError(str(e)) from None
    try:
        validate_definition_prompt(canonical)
    except PromptError as e:
        raise DefinitionError(f"prompt: {e}") from None
    except ValidationError as e:
        raise DefinitionError(_format_validation_error(e)) from None
    return canonical, model, inputs


def _yaml() -> YAML:
    y = YAML(typ="safe", pure=True)
    y.allow_duplicate_keys = False
    return y


def parse_yaml(text: str) -> Any:
    try:
        return _yaml().load(text)
    except YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        problem = getattr(e, "problem", None) or type(e).__name__
        raise DefinitionError(f"invalid YAML{where}: {problem}") from None


@dataclass
class FileResult:
    catalog_id: str
    file: str
    definition: dict[str, Any] | None = None
    model: CatalogDefinition | None = None
    inputs: ExclusionInputs | None = None
    config_rev: str | None = None
    error: str | None = None
    status: str = ""  # accepted | unchanged | invalid | resolve_failed (apply only)
    unresolved: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        if self.model and self.model.name and self.model.name.strip():
            return self.model.name.strip()
        return self.catalog_id.replace("_", " ").title()


def definition_files(directory: str | os.PathLike) -> list[Path]:
    path = Path(directory)
    if not path.is_dir():
        raise LoaderError(f"catalog directory {path} does not exist or is not a directory")
    try:
        entries = sorted(p for p in path.iterdir() if p.suffix in (".yaml", ".yml") and not p.name.startswith("."))
    except OSError as e:
        raise LoaderError(f"catalog directory {path} cannot be read: {type(e).__name__}") from None
    return [p for p in entries if p.is_file()]


def load_file(path: Path) -> FileResult:
    """Parse and validate one file; never raises for problems with its content."""
    catalog_id = path.stem
    result = FileResult(catalog_id=catalog_id, file=path.name)
    try:
        if not CATALOG_ID_RE.fullmatch(catalog_id):
            raise DefinitionError(
                "the file name is the catalog id: lowercase letters and digits joined by '_' (e.g. my_list.yaml)"
            )
        if path.stat().st_size > MAX_FILE_BYTES:
            raise DefinitionError(f"file is larger than {MAX_FILE_BYTES // 1024} KiB")
        text = path.read_text(encoding="utf-8")
        result.definition, result.model, result.inputs = validate_definition(parse_yaml(text))
        result.config_rev = config_rev_of(result.definition)
    except DefinitionError as e:
        result.error = str(e)[:MAX_ERROR_CHARS]
    except (OSError, UnicodeDecodeError) as e:
        result.error = f"cannot read file: {type(e).__name__}"
    return result


def validate_dir(directory: str | os.PathLike | None = None) -> list[FileResult]:
    """Check every definition file; writes nothing and calls no network service."""
    results = [load_file(p) for p in definition_files(directory or settings.CATALOGS_DIR)]
    _check_duplicate_ids(results)
    return results


def _check_duplicate_ids(results: list[FileResult]) -> None:
    seen: dict[str, FileResult] = {}
    for r in results:
        if r.catalog_id in seen:  # my_list.yaml and my_list.yml
            r.error = r.error or f"catalog id {r.catalog_id!r} is also defined by {seen[r.catalog_id].file}"
            r.definition = None
        else:
            seen[r.catalog_id] = r


@dataclass
class ApplyReport:
    files: list[FileResult]
    removed: list[str]

    @property
    def ok(self) -> bool:
        return all(r.error is None for r in self.files)


def _stored(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT catalog_id, name, config_rev, definition_json, state, position, source, exclusions_json "
        "FROM catalog_defs"
    ).fetchall()
    return {r["catalog_id"]: dict(r) for r in rows}


async def _resolve(
    pending: list[FileResult], client_factory: Callable[[], TMDBClient]
) -> dict[str, ResolvedExclusions]:
    out: dict[str, ResolvedExclusions] = {}
    client: TMDBClient | None = None
    try:
        for r in pending:
            try:
                if client is None:
                    client = client_factory()
                out[r.catalog_id] = await resolve_exclusions(client, r.inputs)
            except TMDBError as e:
                r.error = f"exclusion names could not be resolved on TMDB ({e}); not applied"  # controlled text
                r.status = "resolve_failed"
    finally:
        if client is not None:
            await client.aclose()
    return out


async def apply_dir(
    directory: str | os.PathLike | None = None,
    *,
    db_path: str | None = None,
    tmdb_client_factory: Callable[[], TMDBClient] | None = None,
) -> ApplyReport:
    """Validate, resolve exclusions, then write everything in one transaction (module docstring)."""
    files = validate_dir(directory)
    client_factory = tmdb_client_factory or TMDBClient

    with db.session(db_path) as conn:
        stored = _stored(conn)

    resolved: dict[str, ResolvedExclusions] = {}
    pending: list[FileResult] = []
    for r in files:
        if r.error:
            continue
        previous = ResolvedExclusions.from_json((stored.get(r.catalog_id) or {}).get("exclusions_json"))
        if previous is not None and previous.inputs_rev == r.inputs.rev:
            resolved[r.catalog_id] = previous
        elif r.inputs.needs_tmdb:
            pending.append(r)
        else:
            resolved[r.catalog_id] = ResolvedExclusions(inputs_rev=r.inputs.rev)
    if pending:
        resolved.update(await _resolve(pending, client_factory))

    present = {r.catalog_id for r in files}
    removed: list[str] = []
    now = db.now_ms()
    with db.session(db_path) as conn, db.transaction(conn):
        stored = _stored(conn)  # re-read under the write lock
        for index, r in enumerate(files):
            if r.error:
                r.status = r.status or "invalid"
                if r.catalog_id in stored:
                    conn.execute("UPDATE catalog_defs SET last_error = ? WHERE catalog_id = ?", (r.error, r.catalog_id))
                else:
                    conn.execute(
                        "INSERT INTO definition_errors (catalog_id, file, error, at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (catalog_id) DO UPDATE SET file = excluded.file, error = excluded.error, "
                        "at = excluded.at",
                        (r.catalog_id, r.file, r.error, now),
                    )
                continue
            before = stored.get(r.catalog_id)
            state = "enabled" if r.model.enabled else "disabled"
            position = r.model.position if r.model.position is not None else index
            accept_definition(conn, r.catalog_id, r.name, r.definition, state=state, position=position)
            exclusions = resolved[r.catalog_id]
            conn.execute(
                "UPDATE catalog_defs SET exclusions_json = ?, source = 'file' WHERE catalog_id = ?",
                (exclusions.to_json(), r.catalog_id),
            )
            conn.execute("DELETE FROM definition_errors WHERE catalog_id = ?", (r.catalog_id,))
            r.unresolved = list(exclusions.unresolved)
            unchanged = (
                before is not None
                and before["config_rev"] == r.config_rev
                and before["state"] == state
                and before["source"] == "file"
            )
            r.status = "unchanged" if unchanged else "accepted"
        for catalog_id, row in stored.items():
            if catalog_id in present or row["source"] != "file" or row["state"] == "removed":
                continue
            accept_definition(
                conn,
                catalog_id,
                row["name"],
                json.loads(row["definition_json"]),
                state="removed",
                position=row["position"],
            )
            conn.execute("UPDATE catalog_defs SET source = 'file' WHERE catalog_id = ?", (catalog_id,))
            removed.append(catalog_id)
        for row in conn.execute("SELECT catalog_id FROM definition_errors").fetchall():
            if row["catalog_id"] not in present:
                conn.execute("DELETE FROM definition_errors WHERE catalog_id = ?", (row["catalog_id"],))
    return ApplyReport(files=files, removed=removed)


def apply_dir_sync(directory: str | os.PathLike | None = None, **kwargs) -> ApplyReport:
    return asyncio.run(apply_dir(directory, **kwargs))


def report_lines(results: list[FileResult], removed: list[str] = ()) -> list[str]:
    """Human-readable summary for the CLI (our own text only)."""
    lines = []
    for r in results:
        if r.error:
            lines.append(f"{r.file}: ERROR {r.error}")
        else:
            status = r.status or "valid"
            extra = f"; no TMDB match for: {', '.join(r.unresolved)}" if r.unresolved else ""
            lines.append(f"{r.file}: {status} (catalog {r.catalog_id}, rev {r.config_rev[:12]}){extra}")
    lines.extend(f"{c}: removed (file is gone)" for c in removed)
    return lines
