"""
Command line: ``python -m app.curation <command>`` (PLAN.md section 10).

    validate [--dir DIR]            check every catalogs.d/*.yaml; writes nothing
    apply [--dir DIR]               accept valid definitions into SQLite (resolving their
                                    exclusion names on TMDB), record errors, mark removed files
    run ID [--dry-run] [--no-apply] run one refresh (or dry run) through the leased job runner
    rollback ID SNAPSHOT            make an earlier snapshot the active list again
    history clear ID                make previously shown titles eligible again
    migrate                         create or upgrade the database, print the schema version
    backup PATH                     online backup of the database to PATH (safe while running)
    status [--json]                 catalogs, recent runs, schedule, budget, worker heartbeat
    status --check                  exit 0 if this host's worker holds the lease (health check)
    load-fixture PATH [--limit N]   publish a fixture file as a catalog's active snapshot
                                    [--id ID] [--name NAME]

``run`` applies ``catalogs.d`` first (unless ``--no-apply``), queues a job (subject to the
catalog's ``max_runs_per_day`` and the one-open-job rule) and runs it through the same runner
the worker uses: it takes the lease if no live worker holds it (waiting out a dead holder's
lease, at most one TTL), otherwise it leaves the queued job to the worker. Ctrl-C / SIGTERM
abandons the run without publishing and puts the job back.

``load-fixture`` is for testing the serving path (docs/aiometadata-integration.md). It
writes a real activation and history rows, exactly as a refresh would.

Exit codes: 0 success, 1 the run failed or was not applied, 2 usage or input error,
3 refused by a limit (daily runs, an open job of another kind).
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from typing import Optional

from app.core.config import settings
from app.core.privileges import drop_root
from app.curation.budget import BudgetLimits
from app.curation.definitions import LoaderError, apply_dir, report_lines, validate_dir
from app.curation.runner import Runner, Services
from app.store.catalogs import SnapshotItem, StoreError, accept_definition, publish_snapshot
from app.curation.status import collect_status, text_lines, worker_healthy
from app.store.backup import backup_database
from app.store.db import database_path, now_ms, schema_version, session, transaction
from app.store.jobs import Enqueued, enqueue_job
from app.store.maintenance import MaintenanceError, history_clear, rollback


def _load_fixture(args: argparse.Namespace) -> int:
    with open(args.path, encoding="utf-8") as f:
        doc = json.load(f)
    catalog = doc["catalog"]
    catalog_id = args.id or catalog["id"]
    name = args.name or catalog["name"]
    raw_items = doc["items"][: args.limit] if args.limit else doc["items"]
    items = [
        SnapshotItem(
            imdb_id=i["imdb_id"],
            title=i["title"],
            year=i.get("year"),
            tmdb_id=i.get("tmdb_id"),
            poster=i.get("poster"),
            description=i.get("description"),
        )
        for i in raw_items
    ]
    definition = {"source": "fixture", "name": name}
    with session() as conn, transaction(conn):
        rev = accept_definition(conn, catalog_id, name, definition)
        published = publish_snapshot(conn, catalog_id, items, config_rev=rev)
    print(
        f"catalog {catalog_id}: snapshot {published.snapshot_id} active with {len(items)} items "
        f"(activation {published.activation_id})"
    )
    return 0


def _migrate(args: argparse.Namespace) -> int:
    with session() as conn:
        print(f"{database_path()}: schema version {schema_version(conn)}")
    return 0


def _backup(args: argparse.Namespace) -> int:
    with session():
        pass  # create / migrate first, so the backup is of a current schema
    path = backup_database(args.path)
    print(f"backup written to {path}")
    return 0


def _status(args: argparse.Namespace) -> int:
    if args.check:
        if not os.path.exists(database_path()):
            print("unhealthy: no database yet", file=sys.stderr)
            return 1
        with session() as conn:
            ok, message = worker_healthy(conn, now_ms())
        print(("healthy: " if ok else "unhealthy: ") + message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    with session() as conn:
        status = collect_status(conn, now_ms())
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        for line in text_lines(status):
            print(line)
    return 0


def _validate(args: argparse.Namespace) -> int:
    results = validate_dir(args.dir)
    for line in report_lines(results):
        print(line)
    if not results:
        print(f"no definition files in {args.dir or settings.CATALOGS_DIR}")
    return 0 if all(r.error is None for r in results) else 1


async def _apply_async(directory: Optional[str], **kwargs) -> int:
    report = await apply_dir(directory, **kwargs)
    for line in report_lines(report.files, report.removed):
        print(line)
    return 0 if report.ok else 1


def _apply(args: argparse.Namespace) -> int:
    return asyncio.run(_apply_async(args.dir))


# --------------------------------------------------------------------------- run


def _open_job(conn, catalog_id: str):
    return conn.execute(
        "SELECT id, kind, state FROM jobs WHERE catalog_id = ? AND state IN ('pending', 'running')", (catalog_id,)
    ).fetchone()


async def run_command(
    catalog_id: str,
    *,
    dry_run: bool = False,
    apply_first: bool = True,
    directory: Optional[str] = None,
    runner: Optional[Runner] = None,
    services: Optional[Services] = None,
    install_signals: bool = True,
) -> int:
    """``run ID [--dry-run]``; importable so tests can inject fakes through ``services``."""
    directory = directory or settings.CATALOGS_DIR
    services = services or (runner.services if runner else Services())
    if apply_first and os.path.isdir(directory):
        code = await _apply_async(directory, tmdb_client_factory=services.make_tmdb_client)
        if code:
            print("note: some definitions were not applied (see above); continuing with the accepted ones")

    with session() as conn:
        row = conn.execute(
            "SELECT state, definition_json FROM catalog_defs WHERE catalog_id = ?", (catalog_id,)
        ).fetchone()
    if row is None or row["state"] != "enabled":
        print(f"error: catalog {catalog_id!r} is not an enabled catalog", file=sys.stderr)
        return 2
    try:
        limits = BudgetLimits.from_definition(json.loads(row["definition_json"]))
    except ValueError:
        print(f"error: the stored definition of {catalog_id!r} is invalid", file=sys.stderr)
        return 2
    kind = "dry_run" if dry_run else "refresh"

    def enqueue():
        with session() as conn:
            return enqueue_job(conn, catalog_id, kind=kind, trigger="cli", max_runs_per_day=limits.max_runs_per_day)

    runner = runner or Runner(services=services)
    if not await runner.start(wait_s=runner.lease_ttl_ms / 1000 + 1):
        status, job_id = await asyncio.to_thread(enqueue)
        if status is Enqueued.DAILY_LIMIT:
            print(f"refused: {catalog_id} reached max_runs_per_day ({limits.max_runs_per_day})", file=sys.stderr)
            return 3
        print(f"a worker holds the lease; the {kind} job for {catalog_id} is queued for it")
        return 0

    loop = asyncio.get_running_loop()
    installed = []
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, runner.cancel_current, "signal")
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        status, job_id = await asyncio.to_thread(enqueue)
        if status is Enqueued.DAILY_LIMIT:
            print(f"refused: {catalog_id} reached max_runs_per_day ({limits.max_runs_per_day})", file=sys.stderr)
            return 3
        if status is Enqueued.NOT_ENABLED:
            print(f"error: catalog {catalog_id!r} is not enabled", file=sys.stderr)
            return 2
        if status is Enqueued.ALREADY_OPEN:
            with session() as conn:
                open_job = _open_job(conn, catalog_id)
            if open_job is None:
                print("error: the open job disappeared; try again", file=sys.stderr)
                return 1
            if open_job["kind"] != kind:
                print(
                    f"refused: a {open_job['kind']} job (job {open_job['id']}) is already queued for {catalog_id}",
                    file=sys.stderr,
                )
                return 3
            job_id = open_job["id"]
            print(f"running the already queued {kind} job {job_id}")

        report = None
        for _ in range(3):  # a superseded run queues exactly one replacement; follow it
            report = await runner.run_next(job_id)
            if report is None:
                print(f"job {job_id} is no longer runnable (closed or claimed elsewhere)")
                return 1
            if report.outcome != "superseded":
                break
            with session() as conn:
                replacement = _open_job(conn, catalog_id)
            if replacement is None:
                break
            job_id = replacement["id"]
            print("the definition changed during the run; running the replacement job")
        _print_report(report)
        return 0 if report.ok else 1
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        await runner.stop()


def _print_report(report) -> None:
    if report.outcome == "published":
        partial = " (partial list)" if report.partial else ""
        print(f"run {report.run_id}: published {report.items} items for {report.catalog_id}{partial}")
    elif report.outcome == "dry_run_ok":
        print(f"run {report.run_id}: dry run ok, {report.items} items would be published (nothing was changed)")
    elif report.outcome == "cancelled":
        print(f"run {report.run_id}: cancelled; nothing published, job {report.job_id} is queued again")
    elif report.outcome == "lost":
        print(f"run {report.run_id}: lease lost; nothing published")
    else:
        detail = f" ({report.detail})" if report.detail else ""
        state = f"; job is {report.job_state}" if report.job_state else ""
        print(f"run {report.run_id}: {report.outcome}: {report.category}{detail}{state}")


def _run(args: argparse.Namespace) -> int:
    return asyncio.run(run_command(args.id, dry_run=args.dry_run, apply_first=not args.no_apply, directory=args.dir))


# --------------------------------------------------------------------------- maintenance


def _rollback(args: argparse.Namespace) -> int:
    with session() as conn:
        result = rollback(conn, args.id, args.snapshot)
    print(
        f"catalog {args.id}: snapshot {result.snapshot_id} is active again "
        f"(was {result.previous_snapshot_id}); {result.revoked} later activation(s) revoked"
    )
    return 0


def _history_clear(args: argparse.Namespace) -> int:
    with session() as conn:
        revoked = history_clear(conn, args.id)
    print(f"catalog {args.id}: {revoked} activation(s) revoked; the active list stays excluded from the next refresh")
    return 0


def main(argv: list[str] | None = None) -> int:
    drop_root()  # `docker exec` runs as root; never leave root-owned files on the volume
    parser = argparse.ArgumentParser(prog="python -m app.curation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create or upgrade the database").set_defaults(func=_migrate)

    p = sub.add_parser("backup", help="online backup of the database (safe while the services run)")
    p.add_argument("path", help="destination file, e.g. /data/backups/manual.sqlite")
    p.set_defaults(func=_backup)

    p = sub.add_parser("status", help="catalogs, recent runs, schedule, budget and worker heartbeat")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--check", action="store_true", help="exit 0 if this host's worker holds the lease")
    p.set_defaults(func=_status)

    p = sub.add_parser("validate", help="check catalog definition files; writes nothing")
    p.add_argument("--dir", help=f"definitions directory (default: {settings.CATALOGS_DIR})")
    p.set_defaults(func=_validate)

    p = sub.add_parser("apply", help="accept catalog definition files into the database")
    p.add_argument("--dir", help=f"definitions directory (default: {settings.CATALOGS_DIR})")
    p.set_defaults(func=_apply)

    p = sub.add_parser("run", help="run one refresh (or dry run) of a catalog")
    p.add_argument("id")
    p.add_argument("--dry-run", action="store_true", help="run everything, publish nothing")
    p.add_argument("--no-apply", action="store_true", help="do not apply catalogs.d first")
    p.add_argument("--dir", help=f"definitions directory (default: {settings.CATALOGS_DIR})")
    p.set_defaults(func=_run)

    p = sub.add_parser("rollback", help="make an earlier snapshot the active list again")
    p.add_argument("id")
    p.add_argument("snapshot", type=int)
    p.set_defaults(func=_rollback)

    p = sub.add_parser("history", help="history maintenance")
    hist = p.add_subparsers(dest="history_command", required=True)
    h = hist.add_parser("clear", help="make previously shown titles eligible again")
    h.add_argument("id")
    h.set_defaults(func=_history_clear)

    p = sub.add_parser("load-fixture", help="publish a fixture file as a catalog's active snapshot")
    p.add_argument("path")
    p.add_argument("--limit", type=int, help="publish only the first N items")
    p.add_argument("--id", help="catalog id (default: the fixture's)")
    p.add_argument("--name", help="catalog name (default: the fixture's)")
    p.set_defaults(func=_load_fixture)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (StoreError, MaintenanceError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except LoaderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
