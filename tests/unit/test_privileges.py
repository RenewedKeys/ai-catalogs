"""app.core.privileges.drop_root: root -> PUID:PGID; a no-op for non-root and for PUID=0."""

import pytest

from app.core import privileges


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(privileges.os, "setgroups", lambda groups: log.append(("setgroups", groups)))
    monkeypatch.setattr(privileges.os, "setgid", lambda gid: log.append(("setgid", gid)))
    monkeypatch.setattr(privileges.os, "setuid", lambda uid: log.append(("setuid", uid)))
    monkeypatch.delenv("PUID", raising=False)
    monkeypatch.delenv("PGID", raising=False)
    return log


def as_root(monkeypatch, root=True):
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0 if root else 1000)


def test_root_drops_to_the_default_1000(calls, monkeypatch):
    as_root(monkeypatch)
    privileges.drop_root()
    # Order matters: groups and gid must go before the uid (after setuid they cannot be changed).
    assert calls == [("setgroups", []), ("setgid", 1000), ("setuid", 1000)]


def test_root_drops_to_puid_and_pgid(calls, monkeypatch):
    as_root(monkeypatch)
    monkeypatch.setenv("PUID", "1001")
    monkeypatch.setenv("PGID", "1002")
    privileges.drop_root()
    assert calls == [("setgroups", []), ("setgid", 1002), ("setuid", 1001)]


def test_not_root_changes_nothing(calls, monkeypatch):
    as_root(monkeypatch, root=False)
    monkeypatch.setenv("PUID", "1001")
    privileges.drop_root()
    assert calls == []


def test_puid_zero_keeps_root(calls, monkeypatch):
    as_root(monkeypatch)
    monkeypatch.setenv("PUID", "0")
    privileges.drop_root()
    assert calls == []


@pytest.mark.parametrize("value", ["abc", "-1", "10 00"])
def test_bad_ids_are_refused(calls, monkeypatch, value):
    as_root(monkeypatch)
    monkeypatch.setenv("PGID", value)
    with pytest.raises(privileges.PrivilegeError):
        privileges.drop_root()
    assert calls == []


def test_cli_and_worker_drop_root_first(monkeypatch):
    from app import worker
    from app.curation import __main__ as cli

    seen = []
    monkeypatch.setattr(cli, "drop_root", lambda: seen.append("cli"))
    monkeypatch.setattr(worker, "drop_root", lambda: seen.append("worker"))
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert seen == ["cli"]

    class Stop(Exception):
        pass

    def stop(*_a, **_k):
        raise Stop

    monkeypatch.setattr(worker, "Worker", stop)
    with pytest.raises(Stop):
        worker.main()
    assert seen == ["cli", "worker"]
