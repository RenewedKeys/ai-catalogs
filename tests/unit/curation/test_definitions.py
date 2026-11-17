"""The definition loader: catalogs.d/*.yaml -> catalog_defs (PLAN.md sections 3 and 6)."""

import json

import pytest

from app.curation.definitions import LoaderError, apply_dir, parse_yaml, validate_definition, validate_dir
from app.curation.definitions import DefinitionError
from app.curation.exclusions import ExclusionInputs, TitleQuery, pick_named, pick_titles
from app.store import db
from app.store.catalogs import accept_definition
from tests.unit.curation.pipeline_fakes import UniverseTMDB, make_universe

PROMPT = "prompt: |\n  Suggest {{ request_count }} films for category alpha.\n"


def write(d, name, text):
    (d / name).write_text(text, encoding="utf-8")


def row(cid):
    with db.session() as conn:
        r = conn.execute("SELECT * FROM catalog_defs WHERE catalog_id = ?", (cid,)).fetchone()
        return dict(r) if r else None


def errors():
    with db.session() as conn:
        return {r["catalog_id"]: r["error"] for r in conn.execute("SELECT * FROM definition_errors")}


@pytest.fixture
def tmdb():
    named = {
        "collection:Saga Nine": [
            {"id": 900, "name": "Saga Nine Collection"},
            {"id": 901, "name": "Saga Nine: Origins"},
        ],
        "company:Studio Q": [{"id": 31, "name": "Studio Q"}, {"id": 32, "name": "Studio Q Pictures"}],
        "keyword:alpha theme": [],
    }
    return UniverseTMDB(make_universe(50), named)


async def test_apply_accepts_valid_definitions_and_is_idempotent(tmp_path, tmdb):
    write(tmp_path, "first_list.yaml", PROMPT + "target_count: 50\nmin_vote_average: 5.5\n")
    write(tmp_path, "second.yml", PROMPT + "enabled: false\nname: Second\n")
    report = await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert report.ok and [r.status for r in report.files] == ["accepted", "accepted"]
    first = row("first_list")
    assert first["state"] == "enabled" and first["name"] == "First List" and first["source"] == "file"
    assert json.loads(first["definition_json"])["min_vote_average"] == 5.5
    assert row("second")["state"] == "disabled" and row("second")["name"] == "Second"
    epoch = first["epoch"]
    report = await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert [r.status for r in report.files] == ["unchanged", "unchanged"]
    assert row("first_list")["epoch"] == epoch and row("first_list")["config_rev"] == first["config_rev"]
    assert tmdb.requests == []  # no exclusions: TMDB never called


async def test_changed_revision_bumps_epoch(tmp_path, tmdb):
    write(tmp_path, "example.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    before = row("example")
    write(tmp_path, "example.yaml", PROMPT + "target_count: 80\n")
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    after = row("example")
    assert after["config_rev"] != before["config_rev"] and after["epoch"] == before["epoch"] + 1


async def test_invalid_replacement_keeps_the_accepted_definition(tmp_path, tmdb):
    write(tmp_path, "example.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    before = row("example")
    write(tmp_path, "example.yaml", PROMPT + "target_cuont: 80\n")  # typo
    report = await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert not report.ok and report.files[0].status == "invalid"
    after = row("example")
    assert after["config_rev"] == before["config_rev"] and after["epoch"] == before["epoch"]
    assert "target_cuont" in after["last_error"]
    write(tmp_path, "example.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert row("example")["last_error"] is None


async def test_new_invalid_file_is_recorded_then_cleared(tmp_path, tmdb):
    write(tmp_path, "broken.yaml", "prompt: [unclosed\n")
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert row("broken") is None and "invalid YAML" in errors()["broken"]
    write(tmp_path, "broken.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert row("broken")["state"] == "enabled" and errors() == {}


async def test_removed_file_marks_catalog_removed_but_not_fixture_catalogs(tmp_path, tmdb):
    write(tmp_path, "example.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    with db.session() as conn, db.transaction(conn):
        accept_definition(conn, "fixture_list", "Fixture", {"source": "fixture"})
    epoch = row("example")["epoch"]
    (tmp_path / "example.yaml").unlink()
    report = await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert report.removed == ["example"]
    assert row("example")["state"] == "removed" and row("example")["epoch"] == epoch + 1
    assert row("fixture_list")["state"] == "enabled"
    # Re-adding the file brings it back.
    write(tmp_path, "example.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert row("example")["state"] == "enabled"


async def test_missing_directory_changes_nothing(tmp_path, tmdb):
    write(tmp_path, "example.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    with pytest.raises(LoaderError):
        await apply_dir(tmp_path / "missing", tmdb_client_factory=tmdb.client)
    assert row("example")["state"] == "enabled"


async def test_exclusions_are_resolved_automatically_and_only_when_changed(tmp_path, tmdb):
    write(
        tmp_path,
        "example.yaml",
        PROMPT
        + "blacklist_titles:\n  - Film 5\n  - Film 7 (2007)\n  - Missing Title\n"
        + "franchise_denylist:\n  collections: [Saga Nine]\n  companies: [Studio Q]\n  keywords: [alpha theme]\n"
        + "  title_patterns: ['^Film 9$']\n",
    )
    report = await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert report.ok
    excl = json.loads(row("example")["exclusions_json"])
    assert excl["blacklist_tmdb_ids"] == [5]  # Film 7 is from 1987 in the fake universe: no match
    assert excl["collection_ids"] == [900] and excl["company_ids"] == [31]
    assert set(excl["unresolved"]) == {"Film 7 (2007)", "Missing Title", "keyword: alpha theme"}
    assert report.files[0].unresolved
    calls = len(tmdb.requests)
    write(tmp_path, "example.yaml", (tmp_path / "example.yaml").read_text() + "target_count: 90\n")
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert len(tmdb.requests) == calls  # names unchanged: no new resolution


async def test_tmdb_failure_does_not_accept_a_definition_with_exclusions(tmp_path, tmdb):
    write(tmp_path, "example.yaml", PROMPT)
    await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    before = row("example")
    write(tmp_path, "example.yaml", PROMPT + "blacklist_titles: [Film 5]\n")
    tmdb.fail_with = 401
    report = await apply_dir(tmp_path, tmdb_client_factory=tmdb.client)
    assert report.files[0].status == "resolve_failed"
    after = row("example")
    assert after["config_rev"] == before["config_rev"] and "provider_auth" in after["last_error"]


def test_validate_dir_writes_nothing(tmp_path):
    write(tmp_path, "example.yaml", PROMPT)
    write(tmp_path, "Bad-Name.yaml", PROMPT)
    results = validate_dir(tmp_path)
    assert [r.error is None for r in results] == [False, True]
    assert "file name is the catalog id" in results[0].error
    assert row("example") is None


@pytest.mark.parametrize(
    "extra, message",
    [
        ("released: 2020-01-01\n", "plain text"),
        ("target_count: 0\n", "target_count"),
        ("target_count: 300\n", "target_count"),
        ("on_shortfall: publish_partial\n", "min_accept"),
        ("decade_min: {1980: 60, 1990: 60}\n", "decade_min asks for 120"),
        ("year_min: 2000\ndecade_min: {1980: 5}\n", "outside year_min"),
        ("history_mode: loose\n", "history_mode"),
        ("franchise_denylist: {title_patterns: ['(']}\n", "invalid regular expression"),
        ("languages: [english]\n", "ISO 639-1"),
        ("max_tokens: 99999\n", "max_tokens"),
    ],
)
def test_invalid_definitions_are_rejected_with_our_own_message(extra, message):
    with pytest.raises(DefinitionError, match=message):
        validate_definition(parse_yaml(PROMPT + extra))


def test_prompt_template_errors_are_rejected():
    with pytest.raises(DefinitionError, match="unknown variable"):
        validate_definition(parse_yaml("prompt: 'Suggest {{ nonsense }}'\n"))


def test_duplicate_keys_are_rejected():
    with pytest.raises(DefinitionError, match="invalid YAML"):
        parse_yaml(PROMPT + "target_count: 1\ntarget_count: 2\n")


def test_canonical_definition_survives_json():
    canonical, model, inputs = validate_definition(parse_yaml(PROMPT + "decade_min: {1980: 5, '1990s': 3}\n"))
    assert canonical["decade_min"] == {"1980": 5, "1990s": 3}
    assert inputs == ExclusionInputs()


def test_automatic_picks():
    results = [
        {"id": 1, "title": "Title A", "release_date": "1982-01-01", "vote_count": 900},
        {"id": 2, "title": "Title A", "release_date": "2011-01-01", "vote_count": 500},
        {"id": 3, "title": "Title A Returns", "release_date": "1990-01-01", "vote_count": 9000},
        {"id": 4, "title": "Title Ab", "release_date": "1982-01-01", "vote_count": 9000},  # fuzzy only
    ]
    assert pick_titles(TitleQuery("Title A"), results) == [1, 2]  # every exact match without a year
    assert pick_titles(TitleQuery("Title A", 2010), results) == [2]
    assert pick_titles(TitleQuery("Title Abc", None), results) == []  # fuzzy is never auto-picked
    named = [{"id": 10, "name": "Saga Collection"}, {"id": 11, "name": "Saga: Origins Collection"}]
    assert pick_named("Saga", named) == [10]
    assert pick_named("Origins", named) == [11]
    assert pick_named("Awe", [{"id": 1, "name": "Awesome"}]) == []


def test_the_documented_example_is_valid():
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "docs" / "catalog-definition.example.yaml"
    canonical, model, inputs = validate_definition(parse_yaml(path.read_text(encoding="utf-8")))
    assert model.target_count == 100 and inputs.titles
