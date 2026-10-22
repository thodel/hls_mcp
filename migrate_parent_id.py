#!/usr/bin/env python3
"""
migrate_parent_id.py — rename the index columns to what the shared embed_db.py
and vectorstore.py expect (thodel/hls_mcp#14, plan in thodel/ch-h-bot#555).

    python migrate_parent_id.py --db /home/tobias/mcp/data/hls.db

One transaction, idempotent, no re-embed:

    ALTER TABLE chunks         RENAME COLUMN article_id TO parent_id;
    ALTER TABLE embedding_runs RENAME COLUMN n_articles TO n_parents;
    DROP INDEX ix_chunks_article;
    CREATE INDEX ix_chunks_parent ON chunks(parent_id);

then PRAGMA integrity_check. The 57,538 passages keep their ids, offsets, text
and vectors; nothing is embedded again.

DROP INDEX is needed: RENAME COLUMN rewrites the old index to the new column
but keeps its name, and vectorstore.SCHEMA_SQL would then add ix_chunks_parent
beside it — two identical indexes. SQLite cannot rename an index.

The foreign key chunks.parent_id -> articles(id) ON DELETE CASCADE follows the
rename and stays; the shared embed_db.py never turns foreign_keys on, and
dropping it would mean rebuilding the table.

The operator backs the database up first (cp hls.db hls.db.pre-parent-id); this
script does not. The server must be stopped: the old db.py queries article_id
and answers every search_semantic on a renamed database with «no such column».
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

OLD_COLUMN, NEW_COLUMN = "article_id", "parent_id"
OLD_RUNS_COLUMN, NEW_RUNS_COLUMN = "n_articles", "n_parents"
OLD_INDEX, NEW_INDEX = "ix_chunks_article", "ix_chunks_parent"


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    parent = NEW_COLUMN if NEW_COLUMN in columns(conn, "chunks") else OLD_COLUMN
    return {
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "parents": conn.execute(
            f"SELECT COUNT(DISTINCT {parent}) FROM chunks").fetchone()[0],
        "embeddings": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "embedding_runs": conn.execute(
            "SELECT COUNT(*) FROM embedding_runs").fetchone()[0],
    }


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply what is still missing, in one transaction. Returns the steps taken."""
    steps: list[str] = []
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in ("chunks", "embedding_runs"):
        if table not in tables:
            raise SystemExit(f"ERROR: no table {table}; nothing to migrate here")

    conn.isolation_level = None          # explicit transaction below
    conn.execute("BEGIN")
    try:
        if OLD_COLUMN in columns(conn, "chunks"):
            conn.execute(f"ALTER TABLE chunks RENAME COLUMN {OLD_COLUMN} TO {NEW_COLUMN}")
            steps.append(f"chunks.{OLD_COLUMN} -> {NEW_COLUMN}")
        if OLD_RUNS_COLUMN in columns(conn, "embedding_runs"):
            conn.execute(f"ALTER TABLE embedding_runs RENAME COLUMN "
                         f"{OLD_RUNS_COLUMN} TO {NEW_RUNS_COLUMN}")
            steps.append(f"embedding_runs.{OLD_RUNS_COLUMN} -> {NEW_RUNS_COLUMN}")
        if OLD_INDEX in indexes(conn, "chunks"):
            conn.execute(f"DROP INDEX {OLD_INDEX}")
            steps.append(f"DROP INDEX {OLD_INDEX}")
        if NEW_INDEX not in indexes(conn, "chunks"):
            conn.execute(f"CREATE INDEX {NEW_INDEX} ON chunks({NEW_COLUMN})")
            steps.append(f"CREATE INDEX {NEW_INDEX} ON chunks({NEW_COLUMN})")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return steps


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=os.environ.get("HLS_DB", "/data/hls.db"))
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"ERROR: database not found at {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        before = counts(conn)
        print("before:", ", ".join(f"{k}={v:,}" for k, v in before.items()))
        steps = migrate(conn)
        for step in steps:
            print(f"  {step}")
        if not steps:
            print("  nothing to do: already on parent_id")
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        after = counts(conn)
        print("after: ", ", ".join(f"{k}={v:,}" for k, v in after.items()))
        print(f"integrity_check: {check}")
        if check != "ok":
            return 2
        if after != before:
            print("ERROR: the counts changed; restore the backup", file=sys.stderr)
            return 3
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
