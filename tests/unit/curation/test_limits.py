"""Limits: one source of truth for the prompt's constraints block and the gate's rules."""

import dataclasses

import pytest
from pydantic import ValidationError

from app.curation.limits import Limits, gate_rules_from_definition, limits_from_definition
from app.curation.tmdb_gate import FranchiseDenylist, GateRules

FULL = {
    "prompt": "placeholder",
    "min_vote_average": 6.5,
    "min_vote_count": 300,
    "year_min": 1970,
    "year_max": 1999,
    "min_runtime": 80,
    "languages": ["EN", "fr"],
    "excluded_genre_ids": [99, 27, 99, 123456],
    "max_per_collection": 1,
    "director_cap": 2,
    "decade_min": {"1980s": 3, 1990: 2},
    "category_min": {"alpha": 4},
    "unrelated_key": "ignored",
}


def test_empty_definition_is_neutral():
    limits = limits_from_definition({"prompt": "placeholder"})
    assert limits.prompt_block() == ""
    assert limits.lines() == []
    assert limits.gate_rules() == GateRules()


def test_zero_values_mean_off_in_both_outputs():
    limits = limits_from_definition({"min_vote_average": 0, "min_vote_count": 0, "min_runtime": 0})
    assert limits.prompt_block() == ""
    assert limits.gate_rules() == GateRules()


def test_full_definition_builds_gate_rules():
    rules = gate_rules_from_definition(FULL, blacklist_tmdb_ids=[11, 12], franchise_denylist=FranchiseDenylist([5]))
    assert rules.min_vote_average == 6.5
    assert rules.min_vote_count == 300
    assert (rules.year_min, rules.year_max) == (1970, 1999)
    assert rules.min_runtime == 80
    assert rules.languages == frozenset({"en", "fr"})
    assert rules.excluded_genre_ids == frozenset({27, 99, 123456})
    assert rules.blacklist_tmdb_ids == frozenset({11, 12})
    assert rules.franchise_denylist.collection_ids == frozenset({5})
    assert rules.include_credits is True  # director cap needs credits


def test_full_definition_renders_every_limit():
    block = limits_from_definition(FULL).prompt_block()
    assert block.startswith("HARD CONSTRAINTS")
    for needle in (
        "between 1970 and 1999",
        "at least 6.5 out of 10",
        "At least 300 user votes",
        "at least 80 minutes",
        "en, fr",
        "Horror, Documentary, genre id 123456",
        "At most 1 film(s) from the same franchise",
        "At most 2 film(s) by the same director",
        "At least 3 film(s) released in the 1980s",
        "At least 2 film(s) released in the 1990s",
        'At least 4 film(s) with category "alpha"',
    ):
        assert needle in block, needle
    assert "ignored" not in block


def test_every_gate_threshold_has_a_prompt_counterpart():
    """Parity: setting any GateRules threshold through a definition changes the prompt too."""
    probes = {
        "min_runtime": {"min_runtime": 90},
        "min_vote_average": {"min_vote_average": 5.0},
        "min_vote_count": {"min_vote_count": 10},
        "languages": {"languages": ["de"]},
        "excluded_genre_ids": {"excluded_genre_ids": [35]},
        "year_min": {"year_min": 2001},
        "year_max": {"year_max": 2002},
        "include_credits": {"director_cap": 1},
    }
    # names resolved at apply time; the type is stated by the system message, not a constraint
    resolved_elsewhere = {"blacklist_tmdb_ids", "franchise_denylist", "media_type"}
    for f in dataclasses.fields(GateRules):
        if f.name in resolved_elsewhere:
            continue
        assert f.name in probes, f"GateRules.{f.name} has no limit field"
        limits = limits_from_definition(probes[f.name])
        assert getattr(limits.gate_rules(), f.name) != getattr(GateRules(), f.name)
        assert limits.prompt_block(), f.name


def test_single_bound_year_text():
    assert "2005 or later" in limits_from_definition({"year_min": 2005}).prompt_block()
    assert "2005 or earlier" in limits_from_definition({"year_max": 2005}).prompt_block()
    assert "Released in 2005." in limits_from_definition({"year_min": 2005, "year_max": 2005}).prompt_block()


def test_template_vars_have_every_key_even_when_unset():
    data = limits_from_definition({}).template_vars()
    for key in Limits.model_fields:
        assert key in data
    assert data["year_min"] is None and data["block"] == "" and data["excluded_genres"] == []


@pytest.mark.parametrize(
    "bad",
    [
        {"year_min": 2000, "year_max": 1990},
        {"min_vote_average": 11},
        {"languages": ["english"]},
        {"decade_min": {"1985": 2}},
        {"decade_min": {"1980s": 0}},
        {"category_min": {"alpha": -1}},
        {"max_per_collection": 0},
    ],
)
def test_invalid_limits_are_rejected(bad):
    with pytest.raises(ValidationError):
        limits_from_definition(bad)
