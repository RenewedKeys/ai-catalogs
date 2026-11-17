"""
Command line: ``python -m app.curation <command>``.

Milestone 2 commands (the full CLI arrives with milestones 5 and 6, see PLAN.md section 10):

    migrate                         create or upgrade the database, print the schema version
    load-fixture PATH [--limit N]   publish a fixture file as a catalog's active snapshot
                                    [--id ID] [--name NAME]

``load-fixture`` is for testing the serving path (and the AIOMetadata integration matrix,
docs/aiometadata-integration.md) before the pipeline exists. It writes a real activation
and history rows, exactly as a refresh would.
"""

import argparse
import json
import sys

from app.store.catalogs import SnapshotItem, StoreError, accept_definition, publish_snapshot
from app.store.db import database_path, schema_version, session, transaction


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.curation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="create or upgrade the database").set_defaults(func=_migrate)

    p = sub.add_parser("load-fixture", help="publish a fixture file as a catalog's active snapshot")
    p.add_argument("path")
    p.add_argument("--limit", type=int, help="publish only the first N items")
    p.add_argument("--id", help="catalog id (default: the fixture's)")
    p.add_argument("--name", help="catalog name (default: the fixture's)")
    p.set_defaults(func=_load_fixture)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except StoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
