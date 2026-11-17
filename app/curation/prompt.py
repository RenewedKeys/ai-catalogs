"""
Prompt templating for curated catalogs (PLAN.md section 6).

A catalog's ``prompt`` is a Jinja2 template rendered in a ``SandboxedEnvironment`` with
``StrictUndefined``: a misspelt variable is an error at validation time, not silent empty
text, and a template cannot reach Python internals.

Variables (always all defined, so validation and production behave the same):

    today, weekday, day_of_month, month_name, year   the run's date
    seed                                             hex string, see ``make_seed``
    target_count, request_count                      list size, and how many to ask for now
    focus                                            ``weekday_focus[weekday]`` or ``focus``, else ""
    categories, anchors                              the definition's lists, seeded shuffle
    limits                                           dict of every limit (None when unset) + ``block``
    limits_block                                     the constraints block (text)
    exclusions                                       history block: "DO NOT INCLUDE:" lines
    already_this_run                                 block of titles produced earlier this run
    unmet                                            block naming unmet quotas (top-up rounds)

Blocks the template does not place itself are appended after it, in this order: limits,
unmet, already-this-run, exclusions. So a plain free-form prompt still gets every limit and
exclusion, and a template that wants them elsewhere just references the variable.

``seed`` = sha256(date, catalog id, per-run nonce). It drives the category and anchor
shuffle here and the final shuffle in selection, and is stored in ``runs``.

The rendered prompt is capped (``max_prompt_chars``, default 24,000). Over the cap, the
exclusion block loses its oldest lines first, then the already-this-run block; the
template text and the constraints block are never cut. If it is still too long,
``PromptError`` is raised (a ``config`` error).
"""

import hashlib
import random
import secrets
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

import jinja2
from jinja2 import StrictUndefined, meta
from jinja2.sandbox import SandboxedEnvironment

from app.curation.limits import Limits, limits_from_definition
from app.curation.tmdb_gate import clean_title

DEFAULT_MAX_PROMPT_CHARS = 24_000
DEFAULT_HISTORY_PROMPT_LIMIT = 150
TITLES_PER_LINE = 12
EXCLUSION_PREFIX = "DO NOT INCLUDE:"
ALREADY_PREFIX = "ALREADY SUGGESTED THIS RUN (do not repeat):"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

TEMPLATE_VARIABLES = frozenset(
    {
        "today",
        "weekday",
        "day_of_month",
        "month_name",
        "year",
        "seed",
        "target_count",
        "request_count",
        "focus",
        "categories",
        "anchors",
        "limits",
        "limits_block",
        "exclusions",
        "already_this_run",
        "unmet",
    }
)

# Blocks appended after the template unless it references them, in this order.
_APPENDABLE = ("limits_block", "unmet", "already_this_run", "exclusions")


class PromptError(ValueError):
    """The template is invalid, fails to render, or cannot fit the size cap."""


_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)


def make_nonce() -> str:
    return secrets.token_hex(8)


def make_seed(day: date, catalog_id: str, nonce: str) -> str:
    """Deterministic for (date, catalog, nonce); independent of PYTHONHASHSEED."""
    digest = hashlib.sha256(f"{day.isoformat()}|{catalog_id}|{nonce}".encode("utf-8")).hexdigest()
    return digest[:16]


def seeded_random(seed: str, purpose: str = "") -> random.Random:
    """A ``random.Random`` derived from the seed; ``purpose`` separates independent streams."""
    return random.Random(int(hashlib.sha256(f"{seed}|{purpose}".encode("utf-8")).hexdigest()[:16], 16))


def seeded_shuffle(items: Iterable[Any], seed: str, purpose: str) -> list[Any]:
    out = list(items)
    seeded_random(seed, purpose).shuffle(out)
    return out


def format_title(title: str, year: int | None) -> str:
    title = clean_title(title)
    return f"{title} ({year})" if year else title


def _title_lines(prefix: str, titles: Sequence[str]) -> list[str]:
    return [f"{prefix} " + "; ".join(titles[i : i + TITLES_PER_LINE]) for i in range(0, len(titles), TITLES_PER_LINE)]


def exclusion_lines(history: Iterable[tuple[str, int | None]], limit: int = DEFAULT_HISTORY_PROMPT_LIMIT) -> list[str]:
    """
    "DO NOT INCLUDE:" lines of at most 12 titles, for the ``limit`` most recent titles.
    ``history`` is ordered most recent first; duplicates are dropped.
    """
    if limit <= 0:
        return []
    seen: set[str] = set()
    titles: list[str] = []
    for title, year in history:
        text = format_title(title, year)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        titles.append(text)
        if len(titles) >= limit:
            break
    return _title_lines(EXCLUSION_PREFIX, titles)


def already_lines(titles: Iterable[tuple[str, int | None]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for title, year in titles:
        text = format_title(title, year)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return _title_lines(ALREADY_PREFIX, out)


def unmet_block(unmet: Sequence[str]) -> str:
    if not unmet:
        return ""
    return "STILL NEEDED in this batch (quotas not yet met):\n" + "\n".join(f"- {u}" for u in unmet)


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    prompt_hash: str  # sha256 of the text, stored in runs.prompt_hash
    seed: str
    exclusion_lines_used: int
    exclusion_lines_total: int
    already_lines_used: int
    already_lines_total: int

    @property
    def truncated(self) -> bool:
        return (
            self.exclusion_lines_used < self.exclusion_lines_total or self.already_lines_used < self.already_lines_total
        )


def compile_template(source: str) -> tuple[jinja2.Template, frozenset[str]]:
    """Parse once; returns the template and the variable names it references."""
    if not isinstance(source, str) or not source.strip():
        raise PromptError("prompt is empty")
    try:
        ast = _env.parse(source)
        template = _env.from_string(source)
    except jinja2.TemplateSyntaxError as e:
        raise PromptError(f"template syntax error on line {e.lineno}: {e.message}") from None
    return template, frozenset(meta.find_undeclared_variables(ast))


def _focus(definition: Mapping[str, Any], weekday: str) -> str:
    weekday_focus = definition.get("weekday_focus") or {}
    if isinstance(weekday_focus, Mapping):
        value = weekday_focus.get(weekday) or weekday_focus.get(weekday.capitalize())
        if value:
            return str(value)
    return str(definition.get("focus") or "")


def build_context(
    definition: Mapping[str, Any],
    *,
    today: date,
    seed: str,
    target_count: int | None = None,
    request_count: int | None = None,
    limits: Limits | None = None,
) -> dict[str, Any]:
    """Every template variable except the three title blocks (filled in by ``render_prompt``)."""
    limits = limits or limits_from_definition(definition)
    weekday = WEEKDAYS[today.weekday()]
    target = int(target_count if target_count is not None else definition.get("target_count") or 100)
    return {
        "today": today.isoformat(),
        "weekday": weekday.capitalize(),
        "day_of_month": today.day,
        "month_name": today.strftime("%B"),
        "year": today.year,
        "seed": seed,
        "target_count": target,
        "request_count": int(request_count if request_count is not None else target),
        "focus": _focus(definition, weekday),
        "categories": seeded_shuffle(definition.get("categories") or [], seed, "categories"),
        "anchors": seeded_shuffle(definition.get("anchors") or [], seed, "anchors"),
        "limits": limits.template_vars(),
        "limits_block": limits.prompt_block(),
    }


def _render(template: jinja2.Template, context: Mapping[str, Any]) -> str:
    try:
        return template.render(**context).strip()
    except jinja2.UndefinedError as e:
        raise PromptError(f"template uses an undefined variable: {e.message}") from None
    except jinja2.exceptions.SecurityError:
        raise PromptError("template tried an operation the sandbox does not allow") from None
    except jinja2.TemplateError as e:
        raise PromptError(f"template failed to render: {type(e).__name__}") from None
    except Exception as e:  # e.g. a filter given the wrong type
        raise PromptError(f"template failed to render: {type(e).__name__}") from None


def _assemble(template, referenced, context, exclusions: list[str], already: list[str], unmet_text: str) -> str:
    ctx = dict(context)
    ctx["exclusions"] = "\n".join(exclusions)
    ctx["already_this_run"] = "\n".join(already)
    ctx["unmet"] = unmet_text
    parts = [_render(template, ctx)]
    for name in _APPENDABLE:
        if name not in referenced and ctx[name]:
            parts.append(ctx[name])
    return "\n\n".join(p for p in parts if p)


def render_prompt(
    definition: Mapping[str, Any],
    *,
    today: date,
    seed: str,
    history: Iterable[tuple[str, int | None]] = (),
    already_this_run: Iterable[tuple[str, int | None]] = (),
    unmet: Sequence[str] = (),
    target_count: int | None = None,
    request_count: int | None = None,
    max_chars: int | None = None,
) -> RenderedPrompt:
    """
    Render a catalog's prompt for one generation batch. ``history`` is the history window's
    titles, most recent first; ``already_this_run`` the titles produced earlier in this run.
    """
    template, referenced = compile_template(definition.get("prompt"))
    limit = int(definition.get("history_prompt_limit", DEFAULT_HISTORY_PROMPT_LIMIT))
    cap = int(max_chars or definition.get("max_prompt_chars") or DEFAULT_MAX_PROMPT_CHARS)
    context = build_context(definition, today=today, seed=seed, target_count=target_count, request_count=request_count)
    excl = exclusion_lines(history, max(limit, 0))
    already = already_lines(already_this_run)
    unmet_text = unmet_block(list(unmet))

    n_excl, n_already = len(excl), len(already)
    text = _assemble(template, referenced, context, excl[:n_excl], already[:n_already], unmet_text)
    # Shrink the exclusion block (oldest lines are last), then the already-this-run block
    # (oldest first: keep the newest lines, which the model is most likely to repeat).
    while len(text) > cap and n_excl > 0:
        n_excl -= 1
        text = _assemble(template, referenced, context, excl[:n_excl], already[len(already) - n_already :], unmet_text)
    while len(text) > cap and n_already > 0:
        n_already -= 1
        text = _assemble(template, referenced, context, excl[:n_excl], already[len(already) - n_already :], unmet_text)
    if len(text) > cap:
        raise PromptError(f"prompt is {len(text)} characters without exclusions; the cap is {cap}")
    return RenderedPrompt(
        text=text,
        prompt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        seed=seed,
        exclusion_lines_used=n_excl,
        exclusion_lines_total=len(excl),
        already_lines_used=n_already,
        already_lines_total=len(already),
    )


SAMPLE_DATE = date(2000, 1, 3)


def validate_definition_prompt(definition: Mapping[str, Any]) -> RenderedPrompt:
    """
    Render the definition's template against a neutral sample context, with every block
    filled, as the definition loader does before accepting a definition. Raises
    ``PromptError`` (or ``pydantic.ValidationError`` for invalid limits).
    """
    limits_from_definition(definition)  # limits must parse before the prompt can use them
    _, referenced = compile_template(definition.get("prompt"))
    unknown = sorted(referenced - TEMPLATE_VARIABLES)
    if unknown:
        # Caught here even inside a branch the sample context does not take.
        raise PromptError(f"template uses unknown variable(s): {', '.join(unknown)}")
    sample_history = [(f"Sample Title {i}", 1990 + i % 30) for i in range(1, 30)]
    sample_run = [(f"Sample Run Title {i}", 2000 + i % 20) for i in range(1, 15)]
    return render_prompt(
        definition,
        today=SAMPLE_DATE,
        seed=make_seed(SAMPLE_DATE, "sample", "sample"),
        history=sample_history,
        already_this_run=sample_run,
        unmet=["At least 1 more film(s) released in the 1990s."],
    )
