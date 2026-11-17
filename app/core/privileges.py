"""
Run as PUID:PGID, never as root (like linuxserver images; see entrypoint.sh).

The entrypoint starts each service as PUID:PGID already. Commands started with ``docker exec``
(the worker's health check, ``docker compose exec worker python -m app.curation ...``) bypass
the entrypoint and run as root, the image's default user; they call ``drop_root()`` first, so
they never create root-owned files (a SQLite -wal/-shm, a backup) that the services could not
open afterwards.
"""

import os


class PrivilegeError(RuntimeError):
    pass


def _id(name: str) -> int:
    raw = os.environ.get(name, "1000").strip() or "1000"
    if not raw.isdigit():
        raise PrivilegeError(f"{name} must be a number (got {raw!r})")
    return int(raw)


def drop_root() -> None:
    """When running as root, switch to PUID:PGID (default 1000:1000). PUID=0 keeps root."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    uid, gid = _id("PUID"), _id("PGID")
    if uid == 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
