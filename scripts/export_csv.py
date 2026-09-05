#!/usr/bin/env python
"""Export the whole store to CSV.

Ships in M1 deliberately. The SQLite file is the entire system of record for food, library,
recipes, goals, and body composition — the owner should never be locked into this project,
and an export written a year later is an export nobody has ever tested.

    python scripts/export_csv.py --out ./export
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import argparse
import csv
import sqlite3
from pathlib import Path

from macro_mcp.store import open_db

#: Everything except SQLite's own bookkeeping.
def user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def export_table(conn: sqlite3.Connection, table: str, out_dir: Path) -> int:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    target = out_dir / f"{table}.csv"
    columns = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    with target.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerows([list(r) for r in rows])
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="export_csv", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="SQLite path (default: $SQLITE_PATH or ./data/macro.db)")
    p.add_argument("--out", default="./export", help="output directory")
    args = p.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = open_db(args.db)
    try:
        total = 0
        for table in user_tables(conn):
            count = export_table(conn, table, out_dir)
            total += count
            print(f"  {table:<20} {count:>6} rows")
        print(f"\nexported {total} rows across tables to {out_dir.resolve()}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
