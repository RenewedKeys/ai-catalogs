"""
Catalog limits: one source of truth for the prompt and the gate (PLAN.md section 6).

A catalog definition sets any of the limits below; every one is off unless set. From the
same parsed ``Limits`` value come:

1. ``gate_rules()``: the ``GateRules`` the gatekeeper enforces in code (the guarantee);
2. ``prompt_block()``: a plain-language constraints block appended to the prompt, so the
   model aims inside the limits and fewer generations are wasted;
3. ``template_vars()``: the ``limits`` template variable, so a prompt can reference a
   limit directly (``{{ limits.year_min }}``). Every key is present, ``None`` when unset,
   so ``{% if limits.year_min %}`` works under ``StrictUndefined``.

Unset limits produce no prompt text and no gate rule. Selection-stage limits
(``max_per_collection``, ``director_cap``, ``decade_min``, ``category_min``) are rendered
into the prompt here and enforced by selection (milestone 5).
"""

import re
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.curation import media
from app.curation.tmdb_gate import FranchiseDenylist, GateRules

TMDB_MOVIE_GENRES = media.TMDB_MOVIE_GENRES  # kept for existing imports

_DECADE_RE = re.compile(r"^(\d{3})0s?$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")


def _decade_key(value: Any) -> int:
    """1980, "1980" and "1980s" all mean the decade starting 1980."""
    if isinstance(value, int) and not isinstance(value, bool):
        year = value
    else:
        m = _DECADE_RE.match(str(value).strip())
        if not m:
            raise ValueError(f"decade must look like 1980 or '1980s', got {value!r}")
        year = int(m.group(1)) * 10
    if year % 10 or not 1870 <= year <= 2100:
        raise ValueError(f"decade must be a year ending in 0, got {value!r}")
    return year


class Limits(BaseModel):
    """The limit fields of a catalog definition. Other definition keys are ignored here."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str = media.MOVIE  # movie | series: wording and genre names only (definitions validates it)
    min_vote_average: float | None = Field(default=None, ge=0, le=10)
    min_vote_count: int | None = Field(default=None, ge=0)
    year_min: int | None = Field(default=None, ge=1870, le=2100)
    year_max: int | None = Field(default=None, ge=1870, le=2100)
    min_runtime: int | None = Field(default=None, ge=0, le=1000)
    languages: tuple[str, ...] = ()
    excluded_genre_ids: tuple[int, ...] = ()
    max_per_collection: int | None = Field(default=None, ge=1)
    director_cap: int | None = Field(default=None, ge=1)
    decade_min: dict[int, int] = Field(default_factory=dict)
    category_min: dict[str, int] = Field(default_factory=dict)

    @field_validator("languages", mode="before")
    @classmethod
    def _languages(cls, v):
        if v is None:
            return ()
        if isinstance(v, str):
            v = [v]
        out = []
        for code in v:
            code = str(code).strip().lower()
            if not _LANGUAGE_RE.match(code):
                raise ValueError(f"language must be an ISO 639-1 code like 'en', got {code!r}")
            if code not in out:
                out.append(code)
        return tuple(sorted(out))

    @field_validator("excluded_genre_ids", mode="before")
    @classmethod
    def _genres(cls, v):
        return tuple(sorted(set(v or ())))

    @field_validator("decade_min", mode="before")
    @classmethod
    def _decades(cls, v):
        return {_decade_key(k): n for k, n in (v or {}).items()}

    @field_validator("category_min", mode="before")
    @classmethod
    def _categories(cls, v):
        return {str(k).strip(): n for k, n in (v or {}).items() if str(k).strip()}

    @field_validator("decade_min", "category_min")
    @classmethod
    def _positive_counts(cls, v):
        for key, n in v.items():
            if not isinstance(n, int) or n < 1:
                raise ValueError(f"quota for {key!r} must be a whole number >= 1")
        return dict(sorted(v.items()))

    @model_validator(mode="after")
    def _ranges(self):
        if self.year_min is not None and self.year_max is not None and self.year_min > self.year_max:
            raise ValueError("year_min must not be greater than year_max")
        return self

    # ----------------------------------------------------------------- outputs

    def gate_rules(
        self,
        blacklist_tmdb_ids: Iterable[int] = (),
        franchise_denylist: FranchiseDenylist | None = None,
    ) -> GateRules:
        """
        The gatekeeper's rules. Exclusion names are resolved to TMDB ids elsewhere (when a
        definition is applied); pass the resolved ids here.
        """
        return GateRules(
            min_runtime=self.min_runtime or 0,
            min_vote_average=self.min_vote_average or 0.0,
            min_vote_count=self.min_vote_count or 0,
            languages=frozenset(self.languages),
            excluded_genre_ids=frozenset(self.excluded_genre_ids),
            year_min=self.year_min,
            year_max=self.year_max,
            blacklist_tmdb_ids=frozenset(blacklist_tmdb_ids),
            franchise_denylist=franchise_denylist or FranchiseDenylist(),
            include_credits=self.director_cap is not None,
            media_type=media.media_type({"type": self.type}),
        )

    def lines(self) -> list[str]:
        """One plain-language sentence per limit that is set, in a fixed order."""
        w = media.words(self.type)
        genre_names = media.genres(self.type)
        out: list[str] = []
        if self.year_min is not None and self.year_max is not None:
            if self.year_min == self.year_max:
                out.append(f"{w.released} in {self.year_min}.")
            else:
                out.append(f"{w.released} between {self.year_min} and {self.year_max} (inclusive).")
        elif self.year_min is not None:
            out.append(f"{w.released} in {self.year_min} or later.")
        elif self.year_max is not None:
            out.append(f"{w.released} in {self.year_max} or earlier.")
        if self.min_vote_average:
            out.append(f"TMDB user rating of at least {self.min_vote_average:g} out of 10.")
        if self.min_vote_count:
            out.append(f"At least {self.min_vote_count} user votes on TMDB (no obscure titles below that).")
        if self.min_runtime:
            out.append(f"Feature length: a runtime of at least {self.min_runtime} minutes.")
        if self.languages:
            out.append(f"Original language (ISO 639-1) is one of: {', '.join(self.languages)}.")
        if self.excluded_genre_ids:
            names = [genre_names.get(g, f"genre id {g}") for g in self.excluded_genre_ids]
            out.append(f"No {w.many} whose genres include: {', '.join(names)}.")
        if self.max_per_collection is not None:
            out.append(
                f"At most {self.max_per_collection} film(s) from the same franchise or collection "
                "(sequels, prequels and remakes count as one collection)."
            )
        if self.director_cap is not None:
            out.append(f"At most {self.director_cap} film(s) by the same director.")
        for decade, n in self.decade_min.items():
            out.append(f"At least {n} {w.one}(s) {w.released_lower} in the {decade}s.")
        for category, n in self.category_min.items():
            out.append(f'At least {n} {w.one}(s) with category "{category}".')
        return out

    def prompt_block(self) -> str:
        """The constraints block appended to the prompt; empty when no limit is set."""
        lines = self.lines()
        if not lines:
            return ""
        return f"HARD CONSTRAINTS (every {media.words(self.type).one} must satisfy all of these):\n" + "\n".join(
            f"- {line}" for line in lines
        )

    def template_vars(self) -> dict[str, Any]:
        """The ``limits`` template variable: every field, ``None``/empty when unset, plus ``block``."""
        data = self.model_dump()
        data["languages"] = list(self.languages)
        data["excluded_genre_ids"] = list(self.excluded_genre_ids)
        names = media.genres(self.type)
        data["excluded_genres"] = [names.get(g, f"genre id {g}") for g in self.excluded_genre_ids]
        data["block"] = self.prompt_block()
        return data


def limits_from_definition(definition: Mapping[str, Any]) -> Limits:
    """Parse the limit fields of a catalog definition dict (raises ``pydantic.ValidationError``)."""
    return Limits.model_validate(dict(definition))


def gate_rules_from_definition(
    definition: Mapping[str, Any],
    *,
    blacklist_tmdb_ids: Iterable[int] = (),
    franchise_denylist: FranchiseDenylist | None = None,
) -> GateRules:
    """``GateRules`` for a catalog definition dict. Milestone 5 calls this per run."""
    return limits_from_definition(definition).gate_rules(blacklist_tmdb_ids, franchise_denylist)
