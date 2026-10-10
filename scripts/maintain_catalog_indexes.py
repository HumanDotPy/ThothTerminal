"""Explicit, finite catalog index installation and definition repair."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "install"))
    parser.add_argument("--catalog", choices=("all", "datasets", "research-library"), default="all")
    parser.add_argument("--blocking-indexes", action="store_true", help="Explicitly allow ordinary index creation")
    parser.add_argument("--statement-timeout-s", type=int, default=300)
    parser.add_argument("--lock-timeout-s", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.statement_timeout_s <= 3600 or not 1 <= args.lock_timeout_s <= 60:
        parser.error("statement timeout must be 1..3600s; lock timeout 1..60s")
    from sqlalchemy import create_engine, text
    from thoth_terminal.db import configured_database_url, database_backend
    from thoth_terminal.catalog_index_maintenance import catalog_index_status, ensure_catalog_indexes
    database_url = configured_database_url()
    if database_backend(database_url) != "postgresql":
        parser.error("catalog index maintenance requires PostgreSQL")
    # Runtime connection hooks configure sessions inside a transaction. Online
    # maintenance needs autocommit from the first statement, without those hooks.
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(text(f"SET lock_timeout='{args.lock_timeout_s}s'"))
            connection.execute(text(f"SET statement_timeout='{args.statement_timeout_s}s'"))
            if args.action == "status":
                result = catalog_index_status(connection)
            else:
                result = ensure_catalog_indexes(connection, catalog=args.catalog, concurrently=not args.blocking_indexes)
            print(json.dumps(result), flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
