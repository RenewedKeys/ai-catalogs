"""scripts/resolve_blacklist.py: generic exclusion lists, YAML output, never touching catalogs.d."""

import argparse
import importlib.util
import logging
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.curation.tmdb_gate import FranchiseDenylist
from tests.unit.curation.tmdb_fake import FakeTMDB

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "resolve_blacklist.py"
spec = importlib.util.spec_from_file_location("resolve_blacklist", SCRIPT)
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)

NAMED = {
    "collection": {
        "Mad Max": [{"id": 8945, "name": "Mad Max Collection"}, {"id": 1, "name": "Unrelated"}],
        "Thor": [{"id": 131296, "name": "Thor Collection"}, {"id": 1252358, "name": "Thorn Collection"}],
    },
    "company": {"Lucasfilm": [{"id": 11928, "name": "Luna-Film"}, {"id": 1, "name": "Lucasfilm Ltd."}]},
    "keyword": {"dc extended universe": [{"id": 229266, "name": "dc extended universe (dceu)"}]},
}


def transport(fake: FakeTMDB):
    def handle(request: httpx.Request) -> httpx.Response:
        for kind, table in NAMED.items():
            if request.url.path.endswith(f"/search/{kind}"):
                fake.requests.append(request)
                return httpx.Response(200, json={"results": table.get(request.url.params["query"], [])})
        return fake.handle(request)

    return httpx.MockTransport(handle)


def args(path, out=None):
    return argparse.Namespace(exclusions=str(path), out=out)


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "script-key")


def write(tmp_path, text):
    p = tmp_path / "exclusions.txt"
    p.write_text(text)
    return p


class TestParse:
    def test_sections(self):
        ex = rb.parse_exclusions(
            "# comment\nThe Thing (1982)\n\n[franchises]\nMad Max  # note\n[companies]\nLucasfilm\n"
            "[keywords]\ndc extended universe\n[title_patterns]\n\\bstar wars\\b\n[titles]\nTotal Recall\n"
        )
        assert ex.titles == [rb.TitleQuery("The Thing", 1982), rb.TitleQuery("Total Recall", None)]
        assert (ex.franchises, ex.companies, ex.keywords) == (["Mad Max"], ["Lucasfilm"], ["dc extended universe"])
        assert ex.title_patterns == [r"\bstar wars\b"]

    def test_pattern_may_contain_hash(self):
        assert rb.parse_exclusions("[title_patterns]\n^#1 fan$\n# a comment\n").title_patterns == ["^#1 fan$"]

    @pytest.mark.parametrize("text", ["[sequels]\nx\n", "[title_patterns]\n(\n"])
    def test_bad_input_is_rejected(self, text):
        with pytest.raises(rb.InputError):
            rb.parse_exclusions(text)

    def test_nothing_is_built_in(self):
        assert not hasattr(rb, "DEFAULT_TITLES") and not hasattr(rb, "FRANCHISES")
        assert rb.parse_exclusions("") == rb.Exclusions()

    def test_no_catalog_data_ships_with_the_tool(self):
        assert not (ROOT / "docs" / "examples").exists()

    def test_patterns_feed_the_gate_denylist(self):
        ex = rb.parse_exclusions("[title_patterns]\n\\bsome saga\\b\n")
        deny = FranchiseDenylist(title_patterns=ex.title_patterns)
        assert deny.title_matches("Some Saga: Part II") and not deny.title_matches("Awesome Sagas")


class TestCandidates:
    def test_same_title_remakes_are_all_listed(self):
        thing = next(s for s in FakeTMDB().searches if s["query"] == "The Thing" and s["primary_release_year"] is None)
        cands = rb.title_candidates(rb.TitleQuery("The Thing"), thing["results"])
        assert [c["id"] for c in cands] == [990003, 1091, 60935]  # by votes
        assert not any(c["fuzzy"] for c in cands)

    def test_year_narrows(self):
        thing = next(s for s in FakeTMDB().searches if s["query"] == "The Thing" and s["primary_release_year"] is None)
        assert [c["id"] for c in rb.title_candidates(rb.TitleQuery("The Thing", 1982), thing["results"])] == [1091]

    def test_superstrings_are_not_candidates(self):
        modern = next(s for s in FakeTMDB().searches if s["query"] == "Modern" and s["primary_release_year"] is None)
        assert rb.title_candidates(rb.TitleQuery("Modern"), modern["results"]) == []

    def test_named_candidates_match_whole_words(self):
        assert [c["id"] for c in rb.named_candidates("Thor", NAMED["collection"]["Thor"])] == [131296]


class TestRun:
    async def test_yaml_output(self, env_key, tmp_path, capsys):
        fake = FakeTMDB()
        path = write(
            tmp_path,
            "Total Recall\nModern\n[franchises]\nMad Max\nThor\n[companies]\nLucasfilm\n"
            "[keywords]\ndc extended universe\n[title_patterns]\n\\bmad max\\b\n",
        )
        assert await rb.run(args(path), transport=transport(fake)) == 0
        out = capsys.readouterr().out
        assert "  - 64635  # Total Recall (2012), 8100 votes" in out
        assert "  - 861  # Total Recall (1990), 7900 votes" in out
        assert "  # Modern: no TMDB match" in out
        assert "    - 8945  # Mad Max Collection (query: Mad Max)" in out
        assert "    - 131296  # Thor Collection (query: Thor)" in out
        assert "    - 1  # Lucasfilm Ltd. (query: Lucasfilm)" in out
        assert "    - 229266  # dc extended universe (dceu) (query: dc extended universe)" in out
        assert "Unrelated" not in out and "Thorn" not in out and "Luna-Film" not in out
        assert r"    - '\bmad max\b'" in out
        assert fake.requests[0].headers["Authorization"] == "Bearer script-key"

    async def test_retry_logs_never_reach_stdout(self, env_key, tmp_path, capsys):
        handler = logging.StreamHandler(sys.stdout)
        logging.getLogger().addHandler(handler)
        fake = FakeTMDB()
        fake.script = [httpx.Response(429, headers={"Retry-After": "0"})]
        try:
            assert await rb.run(args(write(tmp_path, "The Thing (1982)\n")), transport=transport(fake)) == 0
        finally:
            logging.getLogger().removeHandler(handler)
        captured = capsys.readouterr()
        assert "retry" in captured.err
        for line in captured.out.splitlines():
            assert line == "" or line.startswith(("#", " ", "blacklist_tmdb_ids:", "franchise_denylist:")), line

    def test_truncation_is_reported(self):
        cands = [{"id": i, "title": "X", "year": 2000, "vote_count": 100 - i, "fuzzy": False} for i in range(10)]
        text = rb.render_yaml([rb.TitleResult(rb.TitleQuery("X"), cands)], [], today=date(2026, 11, 16))
        assert f"... {10 - rb.MAX_TITLE_CANDIDATES} more not shown" in text

    def test_yaml_ids_parse_back(self):
        result = rb.TitleResult(
            rb.TitleQuery("X"), [{"id": 5, "title": "X\n#y", "year": 1999, "vote_count": 3, "fuzzy": False}]
        )
        text = rb.render_yaml([result], [], today=date(2026, 11, 16))
        assert [int(m) for m in re.findall(r"^  - (\d+)", text, re.M)] == [5]
        assert "X #y" in text  # newlines in titles cannot break out of the comment

    async def test_refuses_catalogs_d(self, env_key, tmp_path):
        target = rb.REPO / "catalogs.d" / "some-catalog.yaml"
        with pytest.raises(SystemExit):
            await rb.run(args(write(tmp_path, "X\n"), out=str(target)))
        assert not target.exists()

    async def test_writes_out_file(self, env_key, tmp_path):
        out = tmp_path / "blacklist.yaml"
        assert await rb.run(args(write(tmp_path, "The Thing (2011)\n"), str(out)), transport=transport(FakeTMDB())) == 0
        assert "  - 60935  # The Thing (2011), 3200 votes" in out.read_text()

    async def test_bad_input_exit_code(self, env_key, tmp_path, capsys):
        assert await rb.run(args(write(tmp_path, "[oops]\n"))) == 2
        assert "unknown section" in capsys.readouterr().err

    async def test_tmdb_failure_reports_status_only(self, env_key, tmp_path, capsys):
        def deny(request):
            return httpx.Response(401, json={"status_message": "Invalid API key: LEAK"})

        assert await rb.run(args(write(tmp_path, "The Thing\n")), transport=httpx.MockTransport(deny)) == 1
        err = capsys.readouterr().err
        assert "HTTP 401" in err and "LEAK" not in err and "script-key" not in err

    def test_cli_subprocess_help_and_missing_key(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "TMDB_API_KEY"}
        env["DATABASE_PATH"] = str(tmp_path / "db.sqlite")
        help_ = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, env=env)
        assert help_.returncode == 0 and "exclusions" in help_.stdout
        nokey = subprocess.run(
            [sys.executable, str(SCRIPT), str(write(tmp_path, "X\n"))], capture_output=True, text=True, env=env
        )
        assert nokey.returncode == 1 and "TMDB_API_KEY is not set" in nokey.stderr and nokey.stdout == ""
