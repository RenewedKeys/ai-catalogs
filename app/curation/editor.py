"""
The browser editor's model side (PLAN.md section 21): form fields generated from the
definition model, form <-> definition conversion, and saving to ``catalog_defs``.

Form fields come from ``CatalogDefinition.model_fields`` in declaration order. A field typed
``Any`` there is validated elsewhere; it takes its type and bounds from the same-named field of
``Limits``, ``LLMSettings`` or ``BudgetLimits``. Anything the editor has no widget for (mappings,
``franchise_denylist``, ``schedule``, a field added later) is edited as a small YAML box, so a
new definition field shows up without changes here.

Round trip: a stored definition rendered into the form and submitted untouched yields the same
canonical definition (and so the same ``config_rev``): keys absent from the stored definition
stay absent while they hold their default, empty values present in it are kept, numbers keep
their int/float form, and textarea line endings are normalised.

Saving runs exactly what ``apply_dir`` runs for a file: ``validate_definition`` (pydantic, limits,
model and budget settings, schedule, feasibility, the prompt render check), exclusion resolution
on TMDB (a failure means nothing is saved), then ``accept_definition`` with ``source = 'editor'``.
"""

import io
import json
from datetime import date
import re
import types
import typing
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, Callable, Literal, Mapping, Optional

from app.curation.budget import BudgetLimits
from app.curation.definitions import CatalogDefinition, DefinitionError, parse_yaml, validate_definition
from app.curation.exclusions import ResolvedExclusions, resolve_exclusions
from app.curation.limits import Limits
from app.curation.llm import LLMSettings
from app.curation.prompt import validate_definition_prompt
from app.curation.tmdb_gate import TMDBClient, TMDBError
from app.store import db
from app.store.catalogs import accept_definition, config_rev_of, validate_catalog_id, StoreError

MAX_YAML_CHARS = 256 * 1024

# Section headings only. A field missing here lands in "Other"; it still gets a widget.
GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Catalog", ("name", "enabled", "position", "prompt")),
    (
        "Prompt variables",
        (
            "focus",
            "weekday_focus",
            "categories",
            "anchors",
            "history_prompt_limit",
            "watched_prompt_limit",
            "max_prompt_chars",
        ),
    ),
    ("Exclusions", ("blacklist_titles", "blacklist_tmdb_ids", "franchise_denylist", "exclude_watched")),
    (
        "Hard limits",
        (
            "min_vote_average",
            "min_vote_count",
            "year_min",
            "year_max",
            "min_runtime",
            "languages",
            "excluded_genre_ids",
        ),
    ),
    ("Selection", ("max_per_collection", "director_cap", "decade_min", "category_min")),
    (
        "Run shape",
        (
            "target_count",
            "overgenerate_factor",
            "history_window_runs",
            "history_mode",
            "on_shortfall",
            "min_accept",
            "max_model_calls_per_run",
            "max_runs_per_day",
            "schedule",
        ),
    ),
    ("Model", ("model", "temperature", "max_tokens", "timeout_s")),
]
OTHER_GROUP = "Other"
TEXTAREA_FIELDS = {"prompt"}

_INT_RE = re.compile(r"^[+-]?\d+$")
_MISSING = object()


@dataclass
class FieldSpec:
    name: str
    group: str
    kind: str  # text | textarea | number | bool | select | lines | yaml
    number_type: Optional[type] = None  # int | float (number, and lines items)
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    choices: list[str] = field(default_factory=list)
    default: Any = None  # the definition model's default (None: the key is optional)
    required: bool = False

    @property
    def slider(self) -> bool:
        return self.kind == "number" and self.minimum is not None and self.maximum is not None

    @property
    def label(self) -> str:
        return self.name.replace("_", " ")

    @property
    def hint(self) -> str:
        parts = []
        if self.minimum is not None or self.maximum is not None:
            lo = "" if self.minimum is None else _num_text(self.minimum)
            hi = "" if self.maximum is None else _num_text(self.maximum)
            parts.append(f"{lo}–{hi}" if lo and hi else (f">= {lo}" if lo else f"<= {hi}"))
        if self.kind == "lines":
            parts.append("one per line")
        if self.kind == "yaml":
            parts.append("YAML")
        if not self.required and self.kind != "bool":
            parts.append("empty = off / default" if self.default in (None, [], {}) else f"default {self.default}")
        return "; ".join(parts)


def _num_text(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def _unwrap_optional(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _bounds(metadata: list) -> tuple[Optional[float], Optional[float]]:
    lo = hi = None
    for m in metadata:
        if getattr(m, "ge", None) is not None:
            lo = m.ge
        if getattr(m, "gt", None) is not None:
            lo = m.gt
        if getattr(m, "le", None) is not None:
            hi = m.le
        if getattr(m, "lt", None) is not None:
            hi = m.lt
    return lo, hi


def _source_field(name: str) -> tuple[Any, list]:
    """Type and constraint metadata for a definition field typed ``Any``."""
    for model in (Limits, LLMSettings):
        info = model.model_fields.get(name)
        if info is not None:
            return info.annotation, list(info.metadata)
    if name in {f.name for f in dataclass_fields(BudgetLimits)}:
        return typing.get_type_hints(BudgetLimits)[name], [_Ge(0)]  # from_definition: whole numbers >= 0
    return Any, []


@dataclass
class _Ge:
    ge: float


def _spec(name: str, group: str) -> FieldSpec:
    info = CatalogDefinition.model_fields[name]
    annotation, metadata = info.annotation, list(info.metadata)
    if annotation is Any:
        annotation, metadata = _source_field(name)
    ann = _unwrap_optional(annotation)
    default = (
        None if info.default_factory is None and info.is_required() else info.get_default(call_default_factory=True)
    )
    spec = FieldSpec(name=name, group=group, kind="yaml", default=default, required=info.is_required())
    origin, args = typing.get_origin(ann), typing.get_args(ann)
    if ann is bool:
        spec.kind = "bool"
    elif ann in (int, float):
        spec.kind, spec.number_type = "number", ann
        spec.minimum, spec.maximum = _bounds(metadata)
        spec.step = 1 if ann is int else 0.1
    elif ann is str:
        spec.kind = "textarea" if name in TEXTAREA_FIELDS else "text"
    elif origin is Literal:
        spec.kind, spec.choices = "select", [str(a) for a in args]
    elif origin in (list, tuple) and args and args[0] in (str, int) and (len(args) == 1 or args[1] is Ellipsis):
        spec.kind, spec.number_type = "lines", (int if args[0] is int else None)
    return spec


def editor_fields() -> list[FieldSpec]:
    """Every definition field, grouped, in the definition model's order within each group."""
    group_of = {name: g for g, names in GROUPS for name in names}
    rank = {name: i for _, names in GROUPS for i, name in enumerate(names)}
    order = [g for g, _ in GROUPS] + [OTHER_GROUP]
    specs = [_spec(name, group_of.get(name, OTHER_GROUP)) for name in CatalogDefinition.model_fields]
    # Stable sort: unlisted fields keep the definition model's order at the end of their group.
    return sorted(specs, key=lambda s: (order.index(s.group), rank.get(s.name, len(rank))))


def grouped_fields() -> list[tuple[str, list[FieldSpec]]]:
    out: dict[str, list[FieldSpec]] = {}
    for s in editor_fields():
        out.setdefault(s.group, []).append(s)
    return list(out.items())


# ------------------------------------------------------------------ YAML


def to_yaml(value: Any) -> str:
    from ruamel.yaml import YAML

    y = YAML(typ="safe", pure=True)
    y.default_flow_style = False
    y.allow_unicode = True
    y.width = 4096
    buf = io.StringIO()
    y.dump(value, buf)
    text = buf.getvalue()
    return text[: -len("...\n")] if text.endswith("\n...\n") else text  # a scalar's document end marker


def definition_yaml(definition: Mapping[str, Any]) -> str:
    """The stored definition as YAML (export and the raw YAML tab); comments are not kept."""
    ordered = {k: definition[k] for k in CatalogDefinition.model_fields if k in definition}
    ordered.update({k: v for k, v in definition.items() if k not in ordered})
    return to_yaml(ordered) if ordered else ""


def parse_definition_yaml(text: str) -> Any:
    if len(text) > MAX_YAML_CHARS:
        raise DefinitionError(f"YAML is larger than {MAX_YAML_CHARS // 1024} KiB")
    return parse_yaml(text.replace("\r\n", "\n"))


# ------------------------------------------------------------------ form <-> definition


def form_values(definition: Mapping[str, Any]) -> dict[str, str]:
    """The text each widget shows for a stored definition."""
    out: dict[str, str] = {}
    for s in editor_fields():
        if s.name not in definition:
            if s.kind == "bool":
                out[s.name] = "on" if s.default else ""
            elif s.kind in ("number", "select") and s.default is not None:
                out[s.name] = str(s.default)
            else:
                out[s.name] = ""
            continue
        v = definition[s.name]
        if v is None:
            out[s.name] = ""
        elif s.kind == "bool":
            out[s.name] = "on" if v is True else ""
            if not isinstance(v, bool):  # e.g. a string in a hand-written file: keep it editable
                out[s.name] = "on" if v else ""
        elif s.kind == "lines" and isinstance(v, list) and all(isinstance(i, (str, int)) for i in v):
            out[s.name] = "\n".join(str(i) for i in v)
        elif s.kind == "lines" and isinstance(v, (str, int)) and not isinstance(v, bool):
            out[s.name] = str(v)  # a single value where a list is also accepted (languages: fr)
        elif s.kind in ("text", "textarea", "select") and isinstance(v, str):
            out[s.name] = v
        elif s.kind == "number" and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[s.name] = str(v)
        else:  # a value of an unexpected shape: show it as YAML in a text box
            out[s.name] = to_yaml(v).strip()
    out.update(schedule_values(definition.get("schedule")))
    return out


def _parse_number(text: str, s: FieldSpec) -> Any:
    if _INT_RE.match(text):
        return int(text)
    try:
        value = float(text)
    except ValueError:
        raise DefinitionError(f"{s.name}: not a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise DefinitionError(f"{s.name}: not a number")
    return value


def _parse_field(s: FieldSpec, raw: Optional[str]) -> Any:
    text = (raw or "").replace("\r\n", "\n")
    if s.kind == "bool":
        return raw is not None and raw != ""
    if s.kind == "textarea":
        return text if text.strip() else _MISSING
    if s.kind in ("text", "select"):
        return text.strip() if text.strip() else _MISSING
    if s.kind == "number":
        return _parse_number(text.strip(), s) if text.strip() else _MISSING
    if s.kind == "lines":
        items = [line.strip() for line in text.split("\n") if line.strip()]
        if not items:
            return _MISSING
        if s.number_type is int:
            if not all(_INT_RE.match(i) for i in items):
                raise DefinitionError(f"{s.name}: one whole number per line")
            return [int(i) for i in items]
        return items
    # yaml
    if not text.strip():
        return _MISSING
    try:
        return parse_definition_yaml(text)
    except DefinitionError as e:
        raise DefinitionError(f"{s.name}: {e}") from None


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {} or v is False


def definition_from_form(form: Mapping[str, Optional[str]], base: Mapping[str, Any]) -> dict[str, Any]:
    """
    Build the definition a submitted form describes. ``base`` is the stored definition the form
    was rendered from (``{}`` for a new catalog). Raises ``DefinitionError`` on unparseable input.
    Keys the editor does not know (none today) are carried over from ``base`` unchanged.
    """
    out: dict[str, Any] = {}
    errors = []
    for s in editor_fields():
        if s.name in FORM_HIDDEN and s.name not in form:
            if s.name in base:
                out[s.name] = base[s.name]  # not on the page: keep it as stored
            continue
        try:
            if s.name == "schedule" and "sched_mode" in form:
                v = schedule_from_form(form, base.get("schedule"))
            else:
                v = _parse_field(s, form.get(s.name))
        except DefinitionError as e:
            errors.append(str(e))
            continue
        if v is _MISSING or (s.kind == "bool" and v is False and s.default is not True):
            if s.name in base and _empty(base[s.name]):
                out[s.name] = base[s.name]  # an explicit empty value in the stored definition
            continue
        if s.kind == "bool" and s.name in base and not isinstance(base[s.name], bool) and bool(base[s.name]) == v:
            out[s.name] = base[s.name]
            continue
        if s.kind == "lines" and s.name in base and not isinstance(base[s.name], list) and v == [base[s.name]]:
            out[s.name] = base[s.name]  # unchanged single value: keep its scalar form
            continue
        if s.name not in base and s.default is not None and v == s.default and type(v) is type(s.default):
            continue  # untouched default of an absent key
        out[s.name] = v
    if errors:
        raise DefinitionError("; ".join(errors))
    known = set(CatalogDefinition.model_fields)
    out.update({k: v for k, v in base.items() if k not in known})
    return out


# ------------------------------------------------------------------ check and save


@dataclass
class CheckResult:
    definition: dict[str, Any]
    config_rev: str
    prompt_preview: str
    exclusions: ResolvedExclusions

    @property
    def unresolved(self) -> list[str]:
        return list(self.exclusions.unresolved)


def _stored_row(conn, catalog_id: str):
    return conn.execute(
        "SELECT catalog_id, name, config_rev, definition_json, state, position, source, exclusions_json, epoch "
        "FROM catalog_defs WHERE catalog_id = ?",
        (catalog_id,),
    ).fetchone()


async def check_definition(
    catalog_id: str,
    raw: Any,
    *,
    tmdb_client_factory: Callable[[], TMDBClient] = TMDBClient,
    db_path: Optional[str] = None,
) -> CheckResult:
    """Validate like ``apply_dir`` and resolve exclusion names (reusing the stored resolution)."""
    canonical, _model, inputs = validate_definition(raw)
    preview = validate_definition_prompt(canonical).text
    with db.session(db_path) as conn:
        row = _stored_row(conn, catalog_id)
    previous = ResolvedExclusions.from_json(row["exclusions_json"] if row else None)
    if previous is not None and previous.inputs_rev == inputs.rev:
        resolved = previous
    elif inputs.needs_tmdb:
        client = None
        try:
            client = tmdb_client_factory()
            resolved = await resolve_exclusions(client, inputs)
        except TMDBError as e:
            reason = TMDB_PROBLEMS.get(e.category, "TMDB didn't answer")
            raise DefinitionError(
                f"Couldn't look up your exclusions on TMDB ({reason}). Nothing was saved; try again."
            ) from None
        finally:
            if client is not None:
                await client.aclose()
    else:
        resolved = ResolvedExclusions(inputs_rev=inputs.rev)
    return CheckResult(canonical, config_rev_of(canonical), preview, resolved)


def display_name(catalog_id: str, definition: Mapping[str, Any]) -> str:
    name = definition.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return catalog_id.replace("_", " ").title()


TMDB_PROBLEMS = {
    "provider_auth": "the TMDB key was refused; check TMDB_API_KEY on the server",
    "provider_rate_limit": "TMDB is busy right now",
    "provider_http_5xx": "TMDB is having problems right now",
    "timeout": "TMDB took too long to answer",
    "transport": "the server couldn't reach TMDB",
}


class ConflictError(DefinitionError):
    """The stored catalog changed since the form was loaded, or it is not editable here."""


def save_checked(
    catalog_id: str, checked: CheckResult, *, base_rev: Optional[str], db_path: Optional[str] = None
) -> str:
    """
    Accept a checked definition as an editor-managed catalog. ``base_rev`` is the revision the
    form was loaded from (``None`` for a new catalog). Returns the new ``config_rev``.
    """
    try:
        validate_catalog_id(catalog_id)
    except StoreError as e:
        raise DefinitionError(str(e)) from None
    defn = checked.definition
    with db.session(db_path) as conn, db.transaction(conn):
        row = _stored_row(conn, catalog_id)
        if base_rev is None:
            if row is not None and row["state"] != "removed":
                raise ConflictError(f"a catalog with the id {catalog_id!r} already exists")
            if row is not None and row["source"] == "file":
                raise ConflictError(f"{catalog_id!r} belongs to a removed file catalog; pick another id")
        else:
            if row is None or row["state"] == "removed":
                raise ConflictError("this catalog no longer exists")
            if row["source"] != "editor":
                raise ConflictError("this catalog is managed by its file; move it to the editor first")
            if row["config_rev"] != base_rev:
                raise ConflictError("the catalog was changed elsewhere since this page was loaded; reload it")
        if row is None or row["state"] == "removed":
            position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM catalog_defs").fetchone()[0]
            if defn.get("position") is not None:
                position = defn["position"]
        else:
            position = row["position"]  # the dashboard's drag order owns it from here on
        state = "enabled" if defn.get("enabled", True) else "disabled"
        rev = accept_definition(conn, catalog_id, display_name(catalog_id, defn), defn, state=state, position=position)
        conn.execute(
            "UPDATE catalog_defs SET exclusions_json = ?, source = 'editor' WHERE catalog_id = ?",
            (checked.exclusions.to_json(), catalog_id),
        )
        conn.execute("DELETE FROM definition_errors WHERE catalog_id = ?", (catalog_id,))
    return rev


def move_to_editor(catalog_id: str, db_path: Optional[str] = None) -> None:
    """File -> editor. The definition is unchanged, so the revision and epoch are too."""
    with db.session(db_path) as conn, db.transaction(conn):
        row = _stored_row(conn, catalog_id)
        if row is None or row["state"] == "removed":
            raise ConflictError("no such catalog")
        if row["source"] == "editor":
            return
        if row["source"] != "file":
            raise ConflictError("only catalogs from catalogs.d can be moved to the editor")
        conn.execute("UPDATE catalog_defs SET source = 'editor' WHERE catalog_id = ?", (catalog_id,))


def set_state(catalog_id: str, state: str, *, base_rev: str, db_path: Optional[str] = None) -> None:
    """Enable, disable or delete (``removed``) an editor-managed catalog."""
    if state not in ("enabled", "disabled", "removed"):
        raise DefinitionError("invalid state")
    with db.session(db_path) as conn, db.transaction(conn):
        row = _stored_row(conn, catalog_id)
        if row is None or row["state"] == "removed":
            raise ConflictError("no such catalog")
        if row["source"] != "editor":
            raise ConflictError("this catalog is managed by its file; move it to the editor first")
        if row["config_rev"] != base_rev:
            raise ConflictError("the catalog was changed elsewhere since this page was loaded; reload it")
        defn = json.loads(row["definition_json"])
        if state != "removed":
            # Keep the stored definition's `enabled` in step with the state.
            if state == "enabled":
                defn.pop("enabled", None)  # default: enabled
            else:
                defn["enabled"] = False
        accept_definition(conn, catalog_id, row["name"], defn, state=state, position=row["position"])
        conn.execute("UPDATE catalog_defs SET source = 'editor' WHERE catalog_id = ?", (catalog_id,))


# ------------------------------------------------------------------ the simple form (PLAN.md sections 26, 27)

# Shown up front, in this order, with plain labels. Everything else is under "Advanced".
BASIC_FIELDS = (
    "name",
    "prompt",
    "target_count",
    "min_vote_average",
    "min_vote_count",
    "year_min",
    "year_max",
    "min_runtime",
    "languages",
    "exclude_watched",
    "blacklist_titles",
)
# Not on the page at all (the dashboard orders catalogs; "Never include" covers TMDB ids).
# A stored value is kept on save.
FORM_HIDDEN = frozenset({"position", "blacklist_tmdb_ids"})

LABELS = {
    "name": "Name",
    "prompt": "What kind of movies do you want?",
    "target_count": "Movies in the catalog",
    "min_vote_average": "Minimum rating",
    "min_vote_count": "Minimum number of ratings",
    "year_min": "Released from",
    "year_max": "Released until",
    "min_runtime": "Minimum length (minutes)",
    "languages": "Original language",
    "exclude_watched": "Skip movies I've watched",
    "blacklist_titles": "Never include",
    "enabled": "Catalog visible",
    "focus": "Extra instruction",
    "weekday_focus": "Extra instruction per weekday",
    "categories": "Themes to spread the picks across",
    "anchors": "Example movies (taste)",
    "history_prompt_limit": "Recent movies to warn the AI about",
    "watched_prompt_limit": "Watched movies to warn the AI about",
    "max_prompt_chars": "Size limit of the AI request",
    "franchise_denylist": "Exclude franchises, studios and keywords",
    "excluded_genre_ids": "Exclude genres",
    "max_per_collection": "Most movies from one franchise",
    "director_cap": "Most movies from one director",
    "decade_min": "Minimum movies per decade",
    "category_min": "Minimum movies per theme",
    "overgenerate_factor": "Ask the AI for extra movies",
    "history_window_runs": "Don't repeat movies from the last … refreshes",
    "history_mode": "When there aren't enough new movies",
    "on_shortfall": "When a refresh comes up short",
    "min_accept": "Smallest catalog worth publishing",
    "max_model_calls_per_run": "Most AI requests per refresh",
    "max_runs_per_day": "Most refreshes per day",
    "model": "AI model",
    "temperature": "Creativity",
    "max_tokens": "Longest AI answer (tokens)",
    "timeout_s": "Give up on the AI after (seconds)",
}

HELP = {
    "prompt": "Describe the movies you want: mood, genres, era, style, what you love about them. "
    "Leave out counts, dates, ratings and exclusions: set those below and they're added for you.",
    "target_count": "How many movies each refresh puts in the catalog. Up to 250.",
    "min_vote_average": "TMDB rating, 0 to 10. Empty: any rating.",
    "min_vote_count": "Leaves out obscure movies with only a handful of ratings. Empty: any.",
    "year_min": "Empty: any year.",
    "year_max": "Empty: any year.",
    "min_runtime": "Empty: any length.",
    "languages": "Only movies originally made in these languages. Empty: any language.",
    "exclude_watched": "Leaves out everything you've watched, using your MDBList history.",
    "blacklist_titles": "Type a title and pick it from the suggestions. These never appear.",
    "enabled": "Off hides the catalog from your apps (AIOMetadata, Stremio …) and pauses its refreshes. "
    "It stays here.",
    "focus": 'One extra sentence sent with every refresh, e.g. "Lean towards hidden gems this time."',
    "weekday_focus": 'A different emphasis per day, e.g. Friday: "something light and funny". '
    "Used instead of the extra instruction on that day. Leave days empty to skip them.",
    "categories": "Optional themes the AI should mix, e.g. heist, space horror, buddy comedy. "
    "Their order is shuffled each refresh so the catalog varies.",
    "anchors": "Movies that show the taste you want. The AI uses them as a reference; "
    "add them to Never include too if they shouldn't appear.",
    "history_prompt_limit": 'How many recently shown movies are listed in the request as "don\'t suggest these". '
    "Default 150. The app also filters repeats out itself; this just saves wasted suggestions.",
    "watched_prompt_limit": "Like the above, for movies you've watched (only with Skip movies I've watched). "
    "Default 50.",
    "max_prompt_chars": "A safety cap on the whole request the app sends to the AI: your description plus the "
    "lists it adds. Not a limit on the description box. Default 24,000 characters; you shouldn't need "
    "to change it.",
    "franchise_denylist": "Type to search TMDB and pick: whole franchises (e.g. a film series), studios or "
    "keywords. Anything that matches never appears.",
    "excluded_genre_ids": "Movies with any of these genres never appear.",
    "max_per_collection": "E.g. 1: at most one movie from any franchise. Empty: no limit.",
    "director_cap": "E.g. 2: at most two movies by the same director. Empty: no limit.",
    "decade_min": "Guarantee a spread across decades. A refresh that can't meet these keeps the " "previous version.",
    "category_min": 'Guarantee some movies for a theme from "Themes to spread the picks across". '
    "Best effort: a refresh still publishes if it can't.",
    "overgenerate_factor": "Some suggestions get rejected by your rules, so the AI is asked for more than "
    "needed. 1.6 asks for 64 to fill 40. Default 1.6.",
    "history_window_runs": "Movies from the last few versions of this catalog are not picked again. "
    "Default 7: with a daily refresh, nothing repeats within a week.",
    "history_mode": "",
    "on_shortfall": "",
    "min_accept": 'Only used with "Publish what was found": a refresh that finds fewer movies than this keeps '
    "the previous version instead.",
    "max_model_calls_per_run": "A run asks the AI once (big catalogs: a few times, about 60 movies per ask). If too "
    'many suggestions fail your rules, it asks again for more: those are the "extra rounds to fill gaps" in a '
    "run's details. This caps all of them per run. Default 8; a typical run uses 1 or 2.",
    "max_runs_per_day": "Caps refreshes of this catalog per day, including Run now and test runs. Default 4. "
    "Protects your API quota from accidents.",
    "model": "Type to search the models your AI provider offers. Empty: the server default.",
    "temperature": "0 to 2. Lower is more predictable, higher more surprising picks. Empty: the model's "
    "default (usually fine).",
    "max_tokens": "The longest answer the AI may give. It doesn't change how many requests you use; it only "
    "stops very long answers being cut off or running away. Default 8,000 is enough for 250 movies.",
    "timeout_s": "How long to wait for one AI answer before giving up and retrying. Default 120.",
}

OPTION_LABELS = {
    "history_mode": {
        "strict": "Keep the previous version (never repeat)",
        "relax": "Allow the oldest repeats back in",
    },
    "on_shortfall": {
        "fail": "Keep the previous version",
        "publish_partial": "Publish what was found",
    },
}

GROUP_TITLES = {
    "Catalog": "General",
    "Prompt variables": "Steering the AI",
    "Exclusions": "Exclusions",
    "Hard limits": "Exclusions",
    "Selection": "Variety",
    "Run shape": "Refreshes",
    "Model": "AI model",
    OTHER_GROUP: "Other",
}
# Sliders use friendlier ranges than the model's hard bounds (typing a value outside still works).
UI_BOUNDS = {
    "year_min": (1900, date.today().year + 1),
    "year_max": (1900, date.today().year + 1),
    "min_runtime": (0, 240),
    "history_window_runs": (0, 30),
    "history_prompt_limit": (0, 300),
    "watched_prompt_limit": (0, 200),
    "overgenerate_factor": (1, 3),
    "max_tokens": (1000, 16000),
    "timeout_s": (10, 300),
}
NO_SLIDER = {"max_prompt_chars"}

# Pickers: the field keeps its plain text format (one value per line, or YAML/JSON); the
# browser script turns it into chips, suggestions or rows and writes the same format back.
WIDGETS = {
    "languages": {"widget": "chips", "source": "languages"},
    "excluded_genre_ids": {"widget": "chips", "source": "genres"},
    "blacklist_titles": {"widget": "chips", "source": "movie"},
    "anchors": {"widget": "chips", "source": "movie"},
    "categories": {"widget": "chips", "source": "free"},
    "franchise_denylist": {"widget": "denylist"},
    "weekday_focus": {"widget": "weekdays"},
    "decade_min": {"widget": "pairs", "source": "decades"},
    "category_min": {"widget": "pairs", "source": "categories"},
    "model": {"widget": "suggest", "source": "model"},
}

LANGUAGES = [
    ("en", "English"),
    ("fr", "French"),
    ("es", "Spanish"),
    ("de", "German"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("zh", "Chinese"),
    ("cn", "Cantonese"),
    ("hi", "Hindi"),
    ("pt", "Portuguese"),
    ("ru", "Russian"),
    ("sv", "Swedish"),
    ("da", "Danish"),
    ("no", "Norwegian"),
    ("fi", "Finnish"),
    ("nl", "Dutch"),
    ("pl", "Polish"),
    ("tr", "Turkish"),
    ("th", "Thai"),
    ("id", "Indonesian"),
    ("ar", "Arabic"),
    ("he", "Hebrew"),
    ("fa", "Persian"),
    ("cs", "Czech"),
    ("hu", "Hungarian"),
    ("el", "Greek"),
    ("ro", "Romanian"),
    ("uk", "Ukrainian"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
    ("ml", "Malayalam"),
    ("tl", "Tagalog"),
    ("vi", "Vietnamese"),
    ("is", "Icelandic"),
]
GENRES = [
    (28, "Action"),
    (12, "Adventure"),
    (16, "Animation"),
    (35, "Comedy"),
    (80, "Crime"),
    (99, "Documentary"),
    (18, "Drama"),
    (10751, "Family"),
    (14, "Fantasy"),
    (36, "History"),
    (27, "Horror"),
    (10402, "Music"),
    (9648, "Mystery"),
    (10749, "Romance"),
    (878, "Science Fiction"),
    (10770, "TV Movie"),
    (53, "Thriller"),
    (10752, "War"),
    (37, "Western"),
]
DECADES = list(range(1920, date.today().year // 10 * 10 + 1, 10))


def label_of(name: str) -> str:
    return LABELS.get(name, name.replace("_", " ").capitalize())


def help_of(spec: "FieldSpec") -> str:
    if spec.name in HELP:
        return HELP[spec.name]
    if spec.minimum is not None and spec.maximum is not None:
        return f"{_num_text(spec.minimum)} to {_num_text(spec.maximum)}."
    if spec.kind == "lines":
        return "One per line."
    return ""


def option_label(field_name: str, value: str) -> str:
    return OPTION_LABELS.get(field_name, {}).get(value, value)


def ui_spec(spec: "FieldSpec") -> "FieldSpec":
    """The spec as the page shows it: friendlier slider ranges, no slider where it confuses."""
    out = FieldSpec(**{f.name: getattr(spec, f.name) for f in dataclass_fields(FieldSpec)})
    if spec.name in UI_BOUNDS and spec.kind == "number":
        out.minimum, out.maximum = UI_BOUNDS[spec.name]
    if spec.name in NO_SLIDER:
        out.minimum = out.maximum = None
    return out


def basic_fields() -> list[FieldSpec]:
    by_name = {s.name: s for s in editor_fields()}
    return [ui_spec(by_name[n]) for n in BASIC_FIELDS if n in by_name]


def advanced_groups() -> list[tuple[str, list[FieldSpec]]]:
    """Every field not shown up front, grouped (the schedule has its own widget)."""
    out: dict[str, list[FieldSpec]] = {}
    for group, specs in grouped_fields():
        rest = [ui_spec(s) for s in specs if s.name not in BASIC_FIELDS and s.name not in FORM_HIDDEN]
        rest = [s for s in rest if s.name != "schedule"]
        if rest:
            out.setdefault(GROUP_TITLES.get(group, group), []).extend(rest)
    return list(out.items())


# --- schedule: "Manually / Every day at / Every week on / Custom" instead of cron

SCHEDULE_MODES = ("manual", "daily", "weekly", "custom")
WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")  # cron 0-6
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _simple_cron(cron: str) -> Optional[tuple[str, int, int, Optional[int]]]:
    """(mode, hour, minute, weekday) for 'M H * * *' and 'M H * * D'; None for anything else."""
    parts = cron.split()
    if len(parts) != 5 or parts[2:4] != ["*", "*"] or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    minute, hour = int(parts[0]), int(parts[1])
    if minute > 59 or hour > 23:
        return None
    if parts[4] == "*":
        return "daily", hour, minute, None
    if parts[4].isdigit() and int(parts[4]) <= 7:
        return "weekly", hour, minute, int(parts[4]) % 7
    return None


def schedule_values(schedule: Any) -> dict[str, str]:
    """The schedule widget's values for a stored ``schedule``."""
    out = {"sched_mode": "manual", "sched_time": "06:00", "sched_day": "5", "sched_tz": "", "sched_cron": ""}
    if not isinstance(schedule, Mapping) or not isinstance(schedule.get("cron"), str):
        if schedule:
            out["sched_mode"] = "custom"
            out["sched_cron"] = to_yaml(schedule).strip()
        return out
    out["sched_tz"] = str(schedule.get("timezone") or "UTC")
    simple = _simple_cron(schedule["cron"])
    if simple is None:
        out["sched_mode"], out["sched_cron"] = "custom", schedule["cron"]
        return out
    mode, hour, minute, day = simple
    out["sched_mode"], out["sched_time"] = mode, f"{hour:02d}:{minute:02d}"
    if day is not None:
        out["sched_day"] = str(day)
    return out


def schedule_from_form(form: Mapping[str, Optional[str]], base: Any) -> Any:
    """The ``schedule`` a submitted widget describes (``_MISSING`` for "manually")."""
    mode = (form.get("sched_mode") or "manual").strip()
    if mode not in SCHEDULE_MODES:
        raise DefinitionError("refresh: choose manually, every day, every week or custom")
    if mode == "manual":
        return _MISSING
    tz = (form.get("sched_tz") or "").strip() or "UTC"
    if mode == "custom":
        cron = " ".join((form.get("sched_cron") or "").split())
        if not cron:
            raise DefinitionError("refresh: enter a cron expression, e.g. 0 6 * * *")
    else:
        m = _HHMM.match((form.get("sched_time") or "").strip())
        if not m:
            raise DefinitionError("refresh: enter a time like 06:00")
        hour, minute = int(m.group(1)), int(m.group(2))
        day = "*"
        if mode == "weekly":
            raw = (form.get("sched_day") or "").strip()
            if not raw.isdigit() or int(raw) > 6:
                raise DefinitionError("refresh: choose a weekday")
            day = raw
        cron = f"{minute} {hour} * * {day}"
    value = {"cron": cron, "timezone": tz}
    # Unchanged in meaning: keep the stored value exactly (spelling, and an omitted timezone).
    if isinstance(base, Mapping) and isinstance(base.get("cron"), str):
        same_tz = str(base.get("timezone") or "UTC") == tz
        same_cron = " ".join(base["cron"].split()) == cron or (
            mode != "custom" and _simple_cron(base["cron"]) == _simple_cron(cron)
        )
        if same_tz and same_cron:
            return dict(base)
    return value


def catalog_id_for(name: str, taken: set[str]) -> str:
    """A new catalog's id, from its name: lowercase words joined by '_', unique."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    base = "_".join(words)[:48].strip("_") or "catalog"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def taken_catalog_ids(db_path: Optional[str] = None) -> set[str]:
    with db.session(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT catalog_id FROM catalog_defs")}
