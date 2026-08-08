import sqlite3

import duckdb
import pytest

from build_tag_index import STAGING_SCHEMA, build_tag_lookup, day_ordinal, load_alias_map, load_tag_dim, resolve_alias_map, scan_posts, write_outputs


def test_resolve_alias_map_collapses_chains():
    resolved = resolve_alias_map([("a", "b"), ("b", "c")])
    assert resolved == {"a": "c", "b": "c"}


def test_resolve_alias_map_breaks_cycles():
    resolved = resolve_alias_map([("a", "b"), ("b", "a")])
    assert set(resolved) == {"a", "b"}
    assert all(v in {"a", "b"} for v in resolved.values())


def test_day_ordinal_caches_and_rejects_garbage():
    cache: dict[str, int] = {}
    assert day_ordinal("2005-01-01T00:00:00.000-05:00", cache) == 0
    assert day_ordinal("2005-01-11T12:00:00.000-05:00", cache) == 10
    assert cache == {"2005-01-01": 0, "2005-01-11": 10}
    assert day_ordinal("not-a-date", cache) is None
    assert day_ordinal("1999-01-01T00:00:00", cache) is None  # before EPOCH


@pytest.fixture
def fake_db(tmp_path):
    path = tmp_path / "fake.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tags (id INTEGER, name TEXT, category INTEGER, post_count INTEGER, is_deprecated INTEGER)")
    conn.execute("CREATE TABLE tag_aliases (antecedent_name TEXT, consequent_name TEXT, status TEXT)")
    conn.execute("CREATE TABLE posts (id INTEGER, created_at TEXT, tag_string TEXT, fav_count INTEGER, score INTEGER, is_deleted INTEGER)")
    conn.executemany(
        "INSERT INTO tags VALUES (?,?,?,?,?)",
        [
            (1, "1girl", 0, 5000, 0),
            (2, "hatsune_miku", 4, 3000, 0),
            (3, "rare_tag", 0, 10, 0),  # below the min_post_count cutoff
            (4, "old_tag", 0, 500, 1),  # deprecated but kept
            (5, "wlop", 1, 4000, 0),  # artist: popular, but the category is not indexed
        ],
    )
    conn.executemany(
        "INSERT INTO tag_aliases VALUES (?,?,?)",
        [("miku", "hatsune_miku", "active"), ("miku_hatsune", "miku", "active"), ("dead_alias", "1girl", "deleted")],
    )
    conn.executemany(
        "INSERT INTO posts VALUES (?,?,?,?,?,?)",
        [
            (1, "2005-01-01T10:00:00.000-05:00", "1girl miku rare_tag wlop", 10, 5, 0),
            (2, "2005-01-01T20:00:00.000-05:00", "1girl miku_hatsune", 20, 7, 0),
            (3, "2005-01-02T10:00:00.000-05:00", "1girl old_tag", 4, 1, 0),
            (4, "2005-01-02T11:00:00.000-05:00", "1girl hatsune_miku", 999, 99, 1),  # deleted, excluded
        ],
    )
    conn.commit()
    conn.close()
    return path


def build(fake_db, tmp_path, include_deleted=False, max_cells=10_000):
    conn = sqlite3.connect(fake_db)
    staging = duckdb.connect(str(tmp_path / "staging.duckdb"))
    staging.execute(STAGING_SCHEMA)
    alias_map = load_alias_map(conn)
    name_to_id, dim_table = load_tag_dim(conn, min_post_count=100)
    scan_posts(
        connection=conn,
        staging=staging,
        tag_lookup=build_tag_lookup(name_to_id, alias_map),
        chunk_size=2,
        max_id=10,
        include_deleted=include_deleted,
        max_cells=max_cells,
        done_chunks=set(),
    )
    out = tmp_path / "out"
    write_outputs(staging, out, dim_table)
    rows = staging.execute(f"SELECT name, day, posts, fav_sum FROM read_parquet('{(out / 'fact_tag_daily.parquet').as_posix()}') JOIN read_parquet('{(out / 'dim_tag.parquet').as_posix()}') USING (tag_id) ORDER BY name, day").fetchall()
    totals = staging.execute(f"SELECT day, posts, fav_sum FROM read_parquet('{(out / 'fact_total_daily.parquet').as_posix()}') ORDER BY day").fetchall()
    staging.close()
    conn.close()
    return rows, totals


def test_scan_aggregates_by_tag_and_day(fake_db, tmp_path):
    rows, _ = build(fake_db, tmp_path)
    by_name = {(name, str(day)): (posts, fav) for name, day, posts, fav in rows}

    # Both posts on Jan 1 collapse into one cell; fav_sum adds up.
    assert by_name[("1girl", "2005-01-01")] == (2, 30)
    assert by_name[("1girl", "2005-01-02")] == (1, 4)


def test_alias_chain_merges_into_canonical_tag(fake_db, tmp_path):
    rows, _ = build(fake_db, tmp_path)
    names = {name for name, _, _, _ in rows}
    # "miku" and "miku_hatsune" both resolve to hatsune_miku, so neither appears
    # on its own and their two posts land on the canonical tag.
    assert "miku" not in names and "miku_hatsune" not in names
    miku = [(str(day), posts) for name, day, posts, _ in rows if name == "hatsune_miku"]
    assert miku == [("2005-01-01", 2)]


def test_rare_tag_dropped_and_deprecated_kept(fake_db, tmp_path):
    rows, _ = build(fake_db, tmp_path)
    names = {name for name, _, _, _ in rows}
    assert "rare_tag" not in names
    assert "old_tag" in names


def test_artist_tags_never_reach_the_dimension(fake_db):
    conn = sqlite3.connect(fake_db)
    try:
        name_to_id, dim_table = load_tag_dim(conn, min_post_count=100)
    finally:
        conn.close()
    # Dropped by category, not by popularity: wlop clears min_post_count easily.
    assert "wlop" not in name_to_id
    assert 1 not in dim_table.column("category").to_pylist()


def test_artist_tags_are_not_counted_while_scanning(fake_db, tmp_path):
    rows, _ = build(fake_db, tmp_path)
    # Post 1 carries `wlop`, so this proves the exclusion survives the scan
    # rather than only the dimension: no cell is aggregated for it at all.
    assert "wlop" not in {name for name, _, _, _ in rows}


def test_deleted_posts_excluded_by_default(fake_db, tmp_path):
    rows, totals = build(fake_db, tmp_path)
    # Post 4 is the only source of fav_count=999; its absence proves exclusion.
    assert all(fav < 999 for _, _, _, fav in rows)
    assert [(str(day), posts) for day, posts, _ in totals] == [("2005-01-01", 2), ("2005-01-02", 1)]


def test_include_deleted_flag_counts_them(fake_db, tmp_path):
    rows, totals = build(fake_db, tmp_path, include_deleted=True)
    assert [(str(day), posts) for day, posts, _ in totals] == [("2005-01-01", 2), ("2005-01-02", 2)]
    by_name = {(name, str(day)): posts for name, day, posts, _ in rows}
    assert by_name[("hatsune_miku", "2005-01-02")] == 1


def test_mid_chunk_flush_does_not_lose_or_double_count(fake_db, tmp_path):
    # max_cells=1 forces a drain after nearly every post, exercising the
    # pending-table path that keeps the OOM fix from corrupting counts.
    rows, totals = build(fake_db, tmp_path, max_cells=1)
    by_name = {(name, str(day)): (posts, fav) for name, day, posts, fav in rows}
    assert by_name[("1girl", "2005-01-01")] == (2, 30)
    assert by_name[("1girl", "2005-01-02")] == (1, 4)
    assert by_name[("hatsune_miku", "2005-01-01")] == (2, 30)
    assert [posts for _, posts, _ in totals] == [2, 1]


def test_pending_rows_are_promoted_only_on_chunk_commit(fake_db, tmp_path):
    conn = sqlite3.connect(fake_db)
    staging = duckdb.connect(str(tmp_path / "staging.duckdb"))
    staging.execute(STAGING_SCHEMA)
    alias_map = load_alias_map(conn)
    name_to_id, _ = load_tag_dim(conn, min_post_count=100)
    scan_posts(
        connection=conn,
        staging=staging,
        tag_lookup=build_tag_lookup(name_to_id, alias_map),
        chunk_size=2,
        max_id=10,
        include_deleted=False,
        max_cells=1,
        done_chunks=set(),
    )
    # Every chunk committed, so nothing may be left behind in pending.
    assert staging.execute("SELECT COUNT(*) FROM staging_tag_daily_pending").fetchone()[0] == 0
    assert staging.execute("SELECT COUNT(*) FROM staging_tag_daily").fetchone()[0] > 0
    staging.close()
    conn.close()


def test_total_daily_counts_posts_not_tag_pairs(fake_db, tmp_path):
    _, totals = build(fake_db, tmp_path)
    # The baseline must be posts-per-day, not tag-occurrences-per-day, or the
    # share denominator would be inflated several-fold.
    assert [posts for _, posts, _ in totals] == [2, 1]
    assert [fav for _, _, fav in totals] == [30, 4]
