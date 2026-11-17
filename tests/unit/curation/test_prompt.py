"""Prompt templating: sandbox, variables, seed, exclusion block, limits block, size cap."""

import os
import subprocess
import sys
from datetime import date

import pytest
from pathlib import Path

from app.curation.prompt import (
    EXCLUSION_PREFIX,
    PromptError,
    TITLES_PER_LINE,
    exclusion_lines,
    make_seed,
    render_prompt,
    seeded_shuffle,
    validate_definition_prompt,
)

ROOT = Path(__file__).resolve().parents[3]
DAY = date(2026, 3, 4)  # a Wednesday
SEED = make_seed(DAY, "catalog_a", "nonce")


def history(n):
    return [(f"Title {i}", 1950 + i % 70) for i in range(n)]


def render(prompt, **kw):
    definition = {"prompt": prompt, **kw.pop("definition", {})}
    return render_prompt(definition, today=DAY, seed=SEED, **kw)


def test_date_and_count_variables():
    text = render(
        "{{ today }} {{ weekday }} {{ day_of_month }} {{ month_name }} {{ year }} {{ target_count }} {{ request_count }}",
        definition={"target_count": 50},
        request_count=20,
    ).text
    assert text == "2026-03-04 Wednesday 4 March 2026 50 20"


def test_focus_from_weekday_focus_then_focus():
    defn = {"weekday_focus": {"wednesday": "focus W"}, "focus": "fallback"}
    assert render("{{ focus }}", definition=defn).text == "focus W"
    assert render("{{ focus }}", definition={"focus": "fallback"}).text == "fallback"
    assert render("[{{ focus }}]").text == "[]"


def test_strict_undefined_and_sandbox():
    with pytest.raises(PromptError, match="undefined"):
        render("{{ not_a_variable }}")
    with pytest.raises(PromptError):
        render("{{ ''.__class__.__mro__[1].__subclasses__() }}")
    with pytest.raises(PromptError, match="syntax"):
        render("{% if %}")
    with pytest.raises(PromptError, match="empty"):
        render_prompt({"prompt": "  "}, today=DAY, seed=SEED)


def test_seed_is_deterministic_and_process_independent():
    assert make_seed(DAY, "catalog_a", "n1") == make_seed(DAY, "catalog_a", "n1")
    assert (
        len({make_seed(DAY, "catalog_a", "n1"), make_seed(DAY, "catalog_b", "n1"), make_seed(DAY, "catalog_a", "n2")})
        == 3
    )
    code = "from datetime import date; from app.curation.prompt import make_seed; print(make_seed(date(2026,3,4),'catalog_a','nonce'))"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": "123", "PYTEST_VERSION": "1"},
        cwd=ROOT,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == SEED


def test_seed_drives_category_and_anchor_shuffle():
    cats = [f"cat{i}" for i in range(10)]
    defn = {"categories": cats, "anchors": ["a1", "a2", "a3", "a4"]}
    one = render("{{ categories|join(',') }}|{{ anchors|join(',') }}", definition=defn).text
    two = render("{{ categories|join(',') }}|{{ anchors|join(',') }}", definition=defn).text
    assert one == two
    assert one.split("|")[0] == ",".join(seeded_shuffle(cats, SEED, "categories"))
    other = render_prompt({"prompt": "{{ categories|join(',') }}", **defn}, today=DAY, seed="ffff0000ffff0000").text
    assert sorted(other.split(",")) == sorted(cats)


def test_exclusion_lines_are_capped_and_chunked():
    lines = exclusion_lines(history(200), limit=150)
    assert all(line.startswith(EXCLUSION_PREFIX) for line in lines)
    counts = [len(line[len(EXCLUSION_PREFIX) :].split("; ")) for line in lines]
    assert max(counts) <= TITLES_PER_LINE
    assert sum(counts) == 150
    assert "Title 0 (1950)" in lines[0]  # most recent first


def test_exclusion_lines_dedupe_and_clean():
    lines = exclusion_lines([("Title\x00 A", 2000), ("title a", 2000), ("Title A", 2000), ("Title B", None)])
    assert lines == [f"{EXCLUSION_PREFIX} Title A (2000); Title B"]


def test_history_prompt_limit_from_definition():
    out = render("x", history=history(100), definition={"history_prompt_limit": 24})
    assert out.text.count("Title ") == 24
    assert render("x", history=history(100), definition={"history_prompt_limit": 0}).text == "x"


def test_blocks_are_appended_unless_template_places_them():
    defn = {"year_min": 1990}
    appended = render(
        "Base prompt.", definition=defn, history=history(3), unmet=["need more"], already_this_run=[("Run T", 2001)]
    )
    parts = appended.text.split("\n\n")
    assert parts[0] == "Base prompt."
    assert parts[1].startswith("HARD CONSTRAINTS") and "1990 or later" in parts[1]
    assert "need more" in parts[2]
    assert "Run T (2001)" in parts[3]
    assert parts[4].startswith(EXCLUSION_PREFIX)

    placed = render("A {{ exclusions }} B {{ limits_block }}", definition=defn, history=history(2))
    assert placed.text.count(EXCLUSION_PREFIX) == 1
    assert placed.text.count("HARD CONSTRAINTS") == 1
    assert placed.text.startswith("A DO NOT INCLUDE:")


def test_no_limits_no_text():
    assert render("Only the prompt.").text == "Only the prompt."


def test_limits_variable_direct_reference():
    out = render(
        "{% if limits.year_min %}from {{ limits.year_min }}{% endif %}{% if limits.year_max %}to {{ limits.year_max }}{% endif %}",
        definition={"year_min": 1980},
    )
    assert out.text.startswith("from 1980")


def test_cap_truncates_exclusions_first():
    full = render("P", history=history(150), already_this_run=history(30))
    cap = len(full.text) - 200
    cut = render("P", history=history(150), already_this_run=history(30), max_chars=cap)
    assert len(cut.text) <= cap
    assert cut.exclusion_lines_used < cut.exclusion_lines_total
    assert cut.already_lines_used == cut.already_lines_total
    assert cut.truncated
    assert "Title 0 (1950)" in cut.text.split(EXCLUSION_PREFIX, 1)[1]  # newest exclusions kept


def test_cap_then_truncates_already_block_and_never_the_constraints():
    defn = {"year_min": 1990, "min_runtime": 80}
    bare = render("P", definition=defn)
    out = render(
        "P", definition=defn, history=history(150), already_this_run=history(60), max_chars=len(bare.text) + 100
    )
    assert out.exclusion_lines_used == 0
    assert out.already_lines_used < out.already_lines_total
    assert bare.text in out.text or "HARD CONSTRAINTS" in out.text
    with pytest.raises(PromptError, match="cap"):
        render("P" * 500, definition=defn, max_chars=100)


def test_default_cap_is_24k():
    huge = [(f"A Long Placeholder Title Number {i}", 2000) for i in range(2000)]
    out = render("P", history=huge, definition={"history_prompt_limit": 2000})
    assert len(out.text) <= 24_000 and out.truncated


def test_prompt_hash_changes_with_text():
    assert render("a").prompt_hash != render("b").prompt_hash


def test_validate_definition_prompt():
    assert validate_definition_prompt(
        {"prompt": "{{ target_count }} {{ exclusions }} {{ unmet }} {{ limits.year_min }}"}
    )
    with pytest.raises(PromptError):
        validate_definition_prompt({"prompt": "{{ typo_variable }}"})
    with pytest.raises(Exception):
        validate_definition_prompt({"prompt": "ok", "year_min": 2000, "year_max": 1990})


def test_validation_rejects_unknown_variable_in_untaken_branch():
    with pytest.raises(PromptError, match="typo_in_branch"):
        validate_definition_prompt({"prompt": "{% if limits.year_min %}{{ typo_in_branch }}{% endif %}ok"})
