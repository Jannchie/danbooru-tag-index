"""Export the wiki alias pool as a small JSON file.

This exists to cut the bundle exporter's dependency on the 46 GB metadata
database. The alias pool is the only thing `export_index_bundle.py` needed
SQLite for, and for the tags actually shipped it is 2.3 MB of JSON -- small
enough to travel as a build artifact, which lets the bundle be rebuilt in CI on
a runner that never sees the database.

The pool feeds *search only*. It is unlabelled by language and built for recall
rather than equivalence: it lists whatever people call a tag, including narrower
and related terms. Display names come from the name maps instead.
"""

import argparse
import json
import sqlite3
from pathlib import Path

import duckdb

from _paths import DANBOORU_DB_PATH, INDEX_DIR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export wiki other_names for the shipped tags as JSON.")
    parser.add_argument("--database", type=str, default=str(DANBOORU_DB_PATH))
    parser.add_argument("--index-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--output", type=str, default=None, help="Default: <index-dir>/wiki_other_names.json")
    return parser.parse_args()


def shipped_tags(index_dir: Path) -> set[str]:
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT name FROM read_parquet('{(index_dir / 'dim_tag.parquet').as_posix()}')").fetchall()
    finally:
        con.close()
    return {str(name) for name, in rows}


def load_other_names(database: Path, wanted: set[str]) -> dict[str, list[str]]:
    """Wiki alias pool for the given tags, keyed by tag name."""
    con = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    out: dict[str, list[str]] = {}
    try:
        query = (
            "SELECT title, other_names FROM wiki_pages "
            "WHERE other_names IS NOT NULL AND other_names NOT IN ('', '[]') AND COALESCE(is_deleted, 0) = 0"
        )
        for title, payload in con.execute(query):
            if title not in wanted:
                continue
            try:
                names = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(names, list) and names:
                out[str(title)] = [str(n) for n in names if n]
    finally:
        con.close()
    return out


def main() -> None:
    args = parse_args()
    index_dir = Path(args.index_dir)
    database = Path(args.database)
    if not database.exists():
        raise SystemExit(f"database not found: {database}")

    wanted = shipped_tags(index_dir)
    pool = load_other_names(database, wanted)
    output = Path(args.output) if args.output else index_dir / "wiki_other_names.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Compact: this is a build artifact, not something anyone reads by hand.
    output.write_text(json.dumps(pool, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    aliases = sum(len(v) for v in pool.values())
    print(f"{output.name}: {len(pool):,} of {len(wanted):,} shipped tags have wiki aliases ({aliases:,} aliases, {output.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
