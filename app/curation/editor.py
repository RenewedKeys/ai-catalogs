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
    return buf.getvalue()


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
        elif s.kind in ("text", "textarea", "select") and isinstance(v, str):
            out[s.name] = v
        elif s.kind == "number" and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[s.name] = str(v)
        else:  # a value of an unexpected shape: show it as YAML in a text box
            out[s.name] = to_yaml(v).strip()
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
        try:
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
            raise DefinitionError(f"exclusion names could not be resolved on TMDB ({e}); not saved") from None
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
        else:
            position = row["position"]
        if defn.get("position") is not None:
            position = defn["position"]
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
