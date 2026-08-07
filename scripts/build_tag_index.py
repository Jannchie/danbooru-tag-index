"""Build the tag popularity index from the Danbooru metadata database.

Scans `posts` once, expands `tag_string` into (tag, day) cells, and writes
compact Parquet fact tables that a site can serve directly:

    dim_tag.parquet          tag_id, name, category, post_count
    fact_tag_daily.parquet   tag_id, day, posts, fav_sum, score_sum
    fact_total_daily.parquet day, posts, fav_sum, score_sum   (site-wide baseline)
    fact_tag_monthly.parquet / fact_total_monthly.parquet     (rollups)
    fact_category_monthly.parquet  category, month, posts, n_eff, active_tags

The site-wide baseline exists because the index is a *share*, not a raw count:
Danbooru's total upload volume grows year over year, so an absolute per-tag
count mostly measures platform growth. share = tag_posts / total_posts.

The category table exists because share has a second bias the first fix does not
address: the tag universe itself expands. Copyright tags compete for one finite
pool of monthly uploads, so a franchise with an unchanged following still bleeds
share as the field fragments. `n_eff` (1/HHI, the effective number of equally
sized competitors) measures that fragmentation directly -- it went 12 -> 56 for
copyright between 2010 and 2026 -- and dividing it out yields an index where 1.0
means "the size of a typical competitor that month".

Python pre-aggregates each chunk in memory (collapsing ~435M tag-post pairs
down to ~60M daily cells) and flushes to a DuckDB staging table, which does the
final cross-chunk GROUP BY. Chunk boundaries split days, hence the final merge.
"""

import argparse
import sqlite3
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from _paths import DANBOORU_DB_PATH, INDEX_DIR, TAG_INDEX_STAGING_PATH

EPOCH = date(2005, 1, 1)
# Packs (tag_id, day_ord) into one int key -- a tuple key costs ~3x the memory
# and this dict holds millions of entries per chunk. A power of two so the
# unpack is a shift and a mask rather than a divmod, which allocates a tuple
# for each of the ~100M drained cells.
DAY_SHIFT = 15
DAY_SPAN = 1 << DAY_SHIFT
DAY_MASK = DAY_SPAN - 1
FLUSH_BATCH = 1_000_000

STAGING_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_tag_daily (
    tag_id INTEGER, day_ord INTEGER, posts INTEGER, fav_sum BIGINT, score_sum BIGINT
);
-- Mid-chunk flushes land here first and are promoted only when the chunk finishes,
-- so a crash mid-chunk leaves no half-counted rows for --resume to duplicate.
CREATE TABLE IF NOT EXISTS staging_tag_daily_pending (
    tag_id INTEGER, day_ord INTEGER, posts INTEGER, fav_sum BIGINT, score_sum BIGINT
);
CREATE TABLE IF NOT EXISTS staging_total_daily (
    day_ord INTEGER, posts INTEGER, fav_sum BIGINT, score_sum BIGINT
);
CREATE TABLE IF NOT EXISTS staging_chunk_done (hi INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS staging_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build tag index fact tables from the Danbooru SQLite database.")
    parser.add_argument("--database", type=str, default=str(DANBOORU_DB_PATH))
    parser.add_argument("--output-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--staging", type=str, default=str(TAG_INDEX_STAGING_PATH))
    parser.add_argument("--min-post-count", type=int, default=100, help="Skip tags rarer than this; the long tail carries no statistical signal.")
    parser.add_argument("--chunk-size", type=int, default=500_000, help="Posts per in-memory aggregation chunk. Lower it if memory is tight.")
    parser.add_argument("--max-id", type=int, default=None, help="Stop after this post id (for smoke tests).")
    parser.add_argument("--include-deleted", action="store_true", help="Count deleted posts too (default: skip them).")
    parser.add_argument("--max-cells", type=int, default=3_000_000, help="Flush the in-memory aggregate once it holds this many (tag, day) cells.")
    parser.add_argument("--resume", action="store_true", help="Keep existing staging data and skip already-finished chunks.")
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be greater than 0")
    if args.min_post_count < 0:
        parser.error("--min-post-count must not be negative")
    if args.max_cells <= 0:
        parser.error("--max-cells must be greater than 0")
    return args


def resolve_alias_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Collapse alias chains (a->b, b->c) into direct mappings (a->c, b->c).

    Cycles are broken by stopping at the first repeated name, so a malformed
    alias loop degrades to an arbitrary-but-stable representative instead of
    hanging.
    """
    direct = dict(pairs)
    resolved: dict[str, str] = {}
    for name in direct:
        if name in resolved:
            continue
        chain: list[str] = []
        current = name
        # Chains are 1-2 links in practice, so scanning `chain` beats keeping a
        # parallel set in step with it.
        while current in direct and current not in chain:
            chain.append(current)
            current = direct[current]
        for link in chain:
            resolved[link] = current
    return resolved


def load_alias_map(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT antecedent_name, consequent_name FROM tag_aliases WHERE status = 'active'").fetchall()
    pairs = [(str(a), str(c)) for a, c in rows if a and c]
    return resolve_alias_map(pairs)


def load_tag_dim(connection: sqlite3.Connection, min_post_count: int) -> tuple[dict[str, int], pa.Table]:
    # Deprecated tags are kept: they were genuinely used at the time and dropping
    # them would punch holes in the history. dim_tag carries the flag so the site
    # can hide them from search while still charting them.
    rows = connection.execute("SELECT id, name, category, post_count, COALESCE(is_deprecated, 0) FROM tags WHERE post_count >= ?", (min_post_count,)).fetchall()
    name_to_id = {str(name): int(tag_id) for tag_id, name, _, _, _ in rows if name}
    table = pa.table(
        {
            "tag_id": pa.array([int(r[0]) for r in rows], pa.int32()),
            "name": pa.array([str(r[1]) for r in rows], pa.string()),
            "category": pa.array([int(r[2] or 0) for r in rows], pa.int8()),
            "post_count": pa.array([int(r[3] or 0) for r in rows], pa.int64()),
            "is_deprecated": pa.array([bool(r[4]) for r in rows], pa.bool_()),
        }
    )
    return name_to_id, table


def day_ordinal(created_at: str, cache: dict[str, int]) -> int | None:
    """Map an ISO timestamp to days-since-EPOCH, caching by date prefix.

    A chunk spans only a few hundred distinct dates, so the cache turns this
    into a dict lookup for virtually every one of the ~11M rows.
    """
    key = created_at[:10]
    ordinal = cache.get(key)
    if ordinal is None:
        try:
            ordinal = (date.fromisoformat(key) - EPOCH).days
        except ValueError:
            return None
        if ordinal < 0 or ordinal >= DAY_SPAN:
            return None
        cache[key] = ordinal
    return ordinal


def drain_cells(staging: duckdb.DuckDBPyConnection, cells: dict[int, list[int]]) -> int:
    """Move `cells` into the pending table in batches, emptying it as we go.

    Building five parallel Python lists for the whole dict at once doubles peak
    memory exactly when the dict is already at its largest -- that is what used
    to blow up on the dense recent-year chunks. Popping in batches keeps the
    overhead bounded to FLUSH_BATCH rows.

    Returns the number of cells drained.
    """
    drained = 0
    while cells:
        tag_ids, day_ords, posts, fav_sums, score_sums = [], [], [], [], []
        for _ in range(min(FLUSH_BATCH, len(cells))):
            packed, (post_count, fav_sum, score_sum) = cells.popitem()
            tag_id, day_ord = packed >> DAY_SHIFT, packed & DAY_MASK
            tag_ids.append(tag_id)
            day_ords.append(day_ord)
            posts.append(post_count)
            fav_sums.append(fav_sum)
            score_sums.append(score_sum)
        tag_batch = pa.table(
            {
                "tag_id": pa.array(tag_ids, pa.int32()),
                "day_ord": pa.array(day_ords, pa.int32()),
                "posts": pa.array(posts, pa.int32()),
                "fav_sum": pa.array(fav_sums, pa.int64()),
                "score_sum": pa.array(score_sums, pa.int64()),
            }
        )
        staging.register("tag_batch", tag_batch)
        staging.execute("INSERT INTO staging_tag_daily_pending SELECT * FROM tag_batch")
        staging.unregister("tag_batch")
        drained += len(tag_ids)
    return drained


def commit_chunk(staging: duckdb.DuckDBPyConnection, cells: dict[int, list[int]], totals: dict[int, list[int]], hi: int) -> None:
    """Finish a chunk: drain what is left, promote pending rows, mark it done."""
    drain_cells(staging, cells)
    total_batch = pa.table(
        {
            "day_ord": pa.array(list(totals.keys()), pa.int32()),
            "posts": pa.array([v[0] for v in totals.values()], pa.int32()),
            "fav_sum": pa.array([v[1] for v in totals.values()], pa.int64()),
            "score_sum": pa.array([v[2] for v in totals.values()], pa.int64()),
        }
    )
    staging.register("total_batch", total_batch)
    staging.execute("BEGIN TRANSACTION")
    staging.execute("INSERT INTO staging_tag_daily SELECT * FROM staging_tag_daily_pending")
    staging.execute("DELETE FROM staging_tag_daily_pending")
    staging.execute("INSERT INTO staging_total_daily SELECT * FROM total_batch")
    staging.execute("INSERT INTO staging_chunk_done VALUES (?)", (hi,))
    staging.execute("COMMIT")
    staging.unregister("total_batch")


def build_tag_lookup(name_to_id: dict[str, int], alias_map: dict[str, str]) -> dict[str, int]:
    """Fold alias resolution and id lookup into one dict, pre-shifted for packing.

    The scan loop runs this lookup ~435M times, so folding two dict hits and a
    multiply into a single hit is worth the setup: the value stored is already
    `tag_id << DAY_SHIFT`, leaving only an add in the loop body.
    """
    lookup = {name: tag_id << DAY_SHIFT for name, tag_id in name_to_id.items()}
    for antecedent, consequent in alias_map.items():
        tag_id = name_to_id.get(consequent)
        if tag_id is not None:
            lookup[antecedent] = tag_id << DAY_SHIFT
    return lookup


def scan_posts(
    connection: sqlite3.Connection,
    staging: duckdb.DuckDBPyConnection,
    tag_lookup: dict[str, int],
    chunk_size: int,
    max_id: int,
    include_deleted: bool,
    max_cells: int,
    done_chunks: set[int],
) -> tuple[int, int]:
    """Scan posts chunk by chunk, aggregating into DuckDB staging tables."""
    day_cache: dict[str, int] = {}
    scanned = 0
    skipped = 0
    # Filtering in SQL avoids decoding a ~250-char tag_string into a Python str
    # for every deleted post just to throw it away.
    where = "id >= ? AND id < ? AND created_at IS NOT NULL AND tag_string IS NOT NULL"
    if not include_deleted:
        where += " AND COALESCE(is_deleted, 0) = 0"
    query = f"SELECT created_at, tag_string, fav_count, score FROM posts WHERE {where}"
    lookup_get = tag_lookup.get

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        task = progress.add_task("Scanning posts", total=max_id)
        for lo in range(0, max_id, chunk_size):
            hi = min(lo + chunk_size, max_id)
            if hi in done_chunks:
                progress.update(task, completed=hi)
                continue

            cells: dict[int, list[int]] = {}
            totals: dict[int, list[int]] = {}
            cells_get = cells.get
            chunk_cells = 0
            for created_at, tag_string, fav_count, score in connection.execute(query, (lo, hi)):
                scanned += 1
                # Hard ceiling on the in-memory dict. Recent years pack far more
                # distinct (tag, day) cells per post than early ones, so a chunk
                # size that fits 2010 will not fit 2025.
                if len(cells) >= max_cells:
                    chunk_cells += drain_cells(staging, cells)
                day_ord = day_ordinal(str(created_at), day_cache)
                if day_ord is None:
                    skipped += 1
                    continue

                fav = int(fav_count or 0)
                sco = int(score or 0)

                total = totals.get(day_ord)
                if total is None:
                    totals[day_ord] = [1, fav, sco]
                else:
                    total[0] += 1
                    total[1] += fav
                    total[2] += sco

                for raw_tag in tag_string.split(" "):
                    base = lookup_get(raw_tag)
                    if base is None:
                        continue
                    packed = base + day_ord
                    cell = cells_get(packed)
                    if cell is None:
                        cells[packed] = [1, fav, sco]
                    else:
                        cell[0] += 1
                        cell[1] += fav
                        cell[2] += sco

            chunk_cells += len(cells)
            commit_chunk(staging, cells, totals, hi)
            progress.update(task, completed=hi, description=f"Scanning posts (last chunk: {chunk_cells:,} cells)")

    return scanned, skipped


AGG = "SUM(posts)::INTEGER AS posts, SUM(fav_sum)::BIGINT AS fav_sum, SUM(score_sum)::BIGINT AS score_sum"


def copy_parquet(staging: duckdb.DuckDBPyConnection, path: Path, body: str) -> Path:
    staging.execute(f"COPY ({body}) TO '{path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    return path


def write_outputs(staging: duckdb.DuckDBPyConnection, output_dir: Path, dim_table: pa.Table) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    staging.register("dim_table", dim_table)
    day = f"DATE '{EPOCH.isoformat()}' + day_ord AS day"

    dim_path = copy_parquet(staging, output_dir / "dim_tag.parquet", "SELECT * FROM dim_table ORDER BY tag_id")

    # ORDER BY tag_id clusters each tag's history into contiguous row groups, so
    # a per-tag read touches one region of the file instead of the whole thing.
    daily_path = copy_parquet(
        staging,
        output_dir / "fact_tag_daily.parquet",
        f"SELECT tag_id, {day}, {AGG} FROM staging_tag_daily GROUP BY tag_id, day_ord ORDER BY tag_id, day_ord",
    )
    total_daily_path = copy_parquet(
        staging,
        output_dir / "fact_total_daily.parquet",
        f"SELECT {day}, {AGG} FROM staging_total_daily GROUP BY day_ord ORDER BY day_ord",
    )
    monthly_path = copy_parquet(
        staging,
        output_dir / "fact_tag_monthly.parquet",
        f"SELECT tag_id, DATE_TRUNC('month', day) AS month, {AGG} FROM read_parquet('{daily_path.as_posix()}') GROUP BY tag_id, month ORDER BY tag_id, month",
    )
    total_monthly_path = copy_parquet(
        staging,
        output_dir / "fact_total_monthly.parquet",
        f"SELECT DATE_TRUNC('month', day) AS month, {AGG} FROM read_parquet('{total_daily_path.as_posix()}') GROUP BY month ORDER BY month",
    )

    # Per-category monthly totals plus the effective number of competitors,
    # 1/HHI over that category's share distribution.
    #
    # This exists because a plain site-wide share is diluted by the growth of the
    # tag universe itself: copyright tags compete for one finite pool of monthly
    # uploads, so a franchise with an unchanged following still loses share as
    # the field fragments (n_eff for copyright went 12 -> 56 between 2010 and
    # 2026). Dividing the within-category share by the average competitor's share
    # -- i.e. multiplying by n_eff -- removes that, giving "how many times the
    # size of a typical competitor this tag was", where 1.0 is exactly average.
    category_monthly_path = copy_parquet(
        staging,
        output_dir / "fact_category_monthly.parquet",
        f"""
        WITH per_month AS (
            SELECT d.category, m.month, m.posts,
                   SUM(m.posts) OVER (PARTITION BY d.category, m.month) AS cat_posts
            FROM read_parquet('{monthly_path.as_posix()}') m
            JOIN read_parquet('{dim_path.as_posix()}') d USING (tag_id)
        )
        SELECT category, month, ANY_VALUE(cat_posts)::BIGINT AS posts,
               (1.0 / SUM(POWER(posts * 1.0 / cat_posts, 2)))::DOUBLE AS n_eff,
               COUNT(*)::INTEGER AS active_tags
        FROM per_month GROUP BY category, month ORDER BY category, month
        """,
    )

    for path in (dim_path, daily_path, total_daily_path, monthly_path, total_monthly_path, category_monthly_path):
        rows = staging.execute(f"SELECT COUNT(*) FROM read_parquet('{path.as_posix()}')").fetchone()[0]
        print(f"{path.name:30} {rows:>12,} rows  {path.stat().st_size / 1e6:>8.1f} MB")


def main() -> None:
    args = parse_args()
    staging_path = Path(args.staging)
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    if not args.resume and staging_path.exists():
        staging_path.unlink()

    connection = sqlite3.connect(f"file:{Path(args.database).as_posix()}?mode=ro", uri=True)
    staging = duckdb.connect(str(staging_path))
    try:
        staging.execute(STAGING_SCHEMA)
        # Anything still pending belongs to a chunk that never finished; that chunk
        # gets rescanned, so keeping these rows would double-count them.
        staging.execute("DELETE FROM staging_tag_daily_pending")
        done_chunks = {row[0] for row in staging.execute("SELECT hi FROM staging_chunk_done").fetchall()}

        alias_map = load_alias_map(connection)
        name_to_id, dim_table = load_tag_dim(connection, args.min_post_count)
        max_id = args.max_id or int(connection.execute("SELECT MAX(id) FROM posts").fetchone()[0]) + 1
        print(f"{len(name_to_id):,} tags (post_count >= {args.min_post_count}), {len(alias_map):,} alias mappings, scanning up to post id {max_id:,}")

        # Chunk boundaries are derived from chunk_size and max_id, so resuming with
        # different values would re-scan overlapping ranges and double-count.
        fingerprint = f"{args.chunk_size}:{max_id}:{args.min_post_count}:{int(args.include_deleted)}"
        stored = staging.execute("SELECT value FROM staging_meta WHERE key = 'fingerprint'").fetchone()
        if stored is None:
            staging.execute("INSERT INTO staging_meta VALUES ('fingerprint', ?)", (fingerprint,))
        elif stored[0] != fingerprint:
            raise SystemExit(f"Staging was built with different settings ({stored[0]} != {fingerprint}). Rerun without --resume to rebuild.")
        if done_chunks:
            print(f"Resuming: {len(done_chunks):,} chunks already staged")

        scanned, skipped = scan_posts(
            connection=connection,
            staging=staging,
            tag_lookup=build_tag_lookup(name_to_id, alias_map),
            chunk_size=args.chunk_size,
            max_id=max_id,
            include_deleted=args.include_deleted,
            max_cells=args.max_cells,
            done_chunks=done_chunks,
        )
        print(f"Scanned {scanned:,} posts, skipped {skipped:,}")
        write_outputs(staging, Path(args.output_dir), dim_table)
    finally:
        staging.close()
        connection.close()


if __name__ == "__main__":
    main()
