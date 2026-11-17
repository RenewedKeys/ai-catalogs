"""scripts/resolve_blacklist.py: candidate listing, YAML output, never touching catalogs.d."""

import argparse
import importlib.util
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.curation.tmdb_gate import FranchiseDenylist
from tests.unit.curation.tmdb_fake import FakeTMDB

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "resolve_blacklist.py"
spec = importlib.util.spec_from_file_location("resolve_blacklist", SCRIPT)
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)

NAMED = {
    "collection": {
        "Mad Max": [{"id": 8945, "name": "Mad Max Collection"}, {"id": 1, "name": "Unrelated"}],
    },
    "company": {"Lucasfilm": [{"id": 1, "name": "Lucasfilm Ltd."}]},
    "keyword": {},
}


def transport(fake: FakeTMDB):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for kind, table in NAMED.items():
            if path.endswith(f"/search/{kind}"):
                fake.requests.append(request)
                return httpx.Response(200, json={"results": table.get(request.url.params["query"], [])})
        return fake.handle(request)

    return httpx.MockTransport(handle)


def args(**kw):
    base = dict(titles=None, out=None, no_defaults=True, no_franchises=True)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def env_key(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "script-key")


class TestCandidates:
    def test_same_title_remakes_are_all_listed_none_chosen(self):
        results = FakeTMDB().searches
        thing = next(s for s in results if s["query"] == "The Thing" and s["primary_release_year"] is None)
        cands = rb.title_candidates(rb.TitleQuery("g", "The Thing"), thing["results"])
        assert [c["id"] for c in cands] == [990003, 1091, 60935]  # by votes, all kept
        assert not any(c["fuzzy"] for c in cands)

    def test_year_narrows(self):
        thing = next(s for s in FakeTMDB().searches if s["query"] == "The Thing" and s["primary_release_year"] is None)
        cands = rb.title_candidates(rb.TitleQuery("g", "The Thing", 1982), thing["results"])
        assert [c["id"] for c in cands] == [1091]

    def test_superstrings_are_not_candidates(self):
        modern = next(s for s in FakeTMDB().searches if s["query"] == "Modern" and s["primary_release_year"] is None)
        assert rb.title_candidates(rb.TitleQuery("g", "Modern"), modern["results"]) == []

    def test_titles_file(self):
        text = "# my anchors\nThe Thing (1982)\n\n[extra]\nTotal Recall  # remake too\n"
        assert rb.parse_titles_file(text) == [
            rb.TitleQuery("taste_anchors", "The Thing", 1982),
            rb.TitleQuery("extra", "Total Recall", None),
        ]

    def test_defaults_cover_the_plan_lists(self):
        titles = {q.title for q in rb.default_queries()}
        assert {"Upgrade", "Escape from L.A.", "Face/Off", "Last Action Hero", "Robot Jox"} <= titles
        assert len(titles) == 39
        assert len(rb.FRANCHISES) == 12

    def test_franchise_patterns_compile_and_match(self):
        deny = FranchiseDenylist(title_patterns=[p for f in rb.FRANCHISES for p in f.title_patterns])
        for title in ["Star Wars: A New Hope", "Aliens", "Alien³", "Furious 7", "Mission: Impossible - Fallout"]:
            assert deny.title_matches(title), title
        for title in ["Alien Nation", "The Thing", "Starship Troopers", "Fast Color"]:
            assert not deny.title_matches(title), title


class TestRun:
    async def test_retry_logs_never_reach_stdout(self, env_key, tmp_path, capsys):
        import logging
        import sys

        handler = logging.StreamHandler(sys.stdout)
        logging.getLogger().addHandler(handler)
        fake = FakeTMDB()
        fake.script = [httpx.Response(429, headers={"Retry-After": "0"})]
        titles = tmp_path / "t.txt"
        titles.write_text("The Thing (1982)\n")
        try:
            assert await rb.run(args(titles=str(titles)), transport=transport(fake)) == 0
        finally:
            logging.getLogger().removeHandler(handler)
        captured = capsys.readouterr()
        assert "retry" in captured.err
        for line in captured.out.splitlines():
            assert line == "" or line.startswith(("#", " ", "blacklist_tmdb_ids:", "franchise_denylist:")), line

    def test_truncation_is_reported(self):
        cands = [{"id": i, "title": "X", "year": 2000, "vote_count": 100 - i, "fuzzy": False} for i in range(10)]
        text = rb.render_yaml([rb.TitleResult(rb.TitleQuery("g", "X"), cands)], [], today=date(2026, 11, 16))
        assert f"... {10 - rb.MAX_TITLE_CANDIDATES} more not shown" in text

    def test_cli_subprocess_help_and_missing_key(self, tmp_path):
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k != "TMDB_API_KEY"}
        env["DATABASE_PATH"] = str(tmp_path / "db.sqlite")
        help_ = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, env=env)
        assert help_.returncode == 0 and "--titles" in help_.stdout
        nokey = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)
        assert nokey.returncode == 1 and "TMDB_API_KEY is not set" in nokey.stderr and nokey.stdout == ""

    async def test_yaml_output(self, env_key, tmp_path, capsys):
        fake = FakeTMDB()
        titles = tmp_path / "anchors.txt"
        titles.write_text("Total Recall\nModern\n")
        franchises = (rb.Franchise("Mad Max", collections=("Mad Max",), title_patterns=(r"\bmad max\b",)),)
        rb.FRANCHISES, saved = franchises, rb.FRANCHISES
        try:
            assert await rb.run(args(titles=str(titles), no_franchises=False), transport=transport(fake)) == 0
        finally:
            rb.FRANCHISES = saved
        out = capsys.readouterr().out
        assert "  - 64635  # Total Recall (2012), 8100 votes" in out
        assert "  - 861  # Total Recall (1990), 7900 votes" in out
        assert "  # Modern: no TMDB match, resolve by hand" in out
        assert "    - 8945  # Mad Max Collection (query: Mad Max)" in out
        assert "Unrelated" not in out
        assert r"    - '\bmad max\b'" in out
        assert fake.requests[0].headers["Authorization"] == "Bearer script-key"

    def test_yaml_ids_parse_back(self):
        result = rb.TitleResult(
            rb.TitleQuery("g", "X"), [{"id": 5, "title": "X\n#y", "year": 1999, "vote_count": 3, "fuzzy": False}]
        )
        text = rb.render_yaml([result], [], today=date(2026, 11, 16))
        ids = [int(m) for m in re.findall(r"^  - (\d+)", text, re.M)]
        assert ids == [5]
        assert "X #y" in text  # newlines in titles cannot break out of the comment

    async def test_refuses_catalogs_d(self, env_key):
        with pytest.raises(SystemExit):
            await rb.run(args(out=str(rb.REPO / "catalogs.d" / "daily-cult-picks.yaml")))
        assert not (rb.REPO / "catalogs.d" / "daily-cult-picks.yaml").exists()

    async def test_writes_out_file(self, env_key, tmp_path):
        out = tmp_path / "blacklist.yaml"
        fake = FakeTMDB()
        titles = tmp_path / "t.txt"
        titles.write_text("The Thing (2011)\n")
        assert await rb.run(args(titles=str(titles), out=str(out)), transport=transport(fake)) == 0
        assert "  - 60935  # The Thing (2011), 3200 votes" in out.read_text()

    async def test_tmdb_failure_reports_status_only(self, env_key, tmp_path, capsys):
        def deny(request):
            return httpx.Response(401, json={"status_message": "Invalid API key: LEAK"})

        titles = tmp_path / "t.txt"
        titles.write_text("The Thing\n")
        assert await rb.run(args(titles=str(titles)), transport=httpx.MockTransport(deny)) == 1
        err = capsys.readouterr().err
        assert "HTTP 401" in err and "LEAK" not in err and "script-key" not in err
