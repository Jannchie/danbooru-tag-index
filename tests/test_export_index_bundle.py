import struct

import duckdb
import pytest

from export_index_bundle import (
    ALIAS_CODE,
    HEADER_FORMAT,
    HEADER_SIZE,
    LANGS,
    MAGIC,
    RECORD_FORMAT,
    TAG_RECORD_SIZE,
    VERSION,
    Converters,
    assemble,
    build_sections,
    complete_month_range,
    encode_varint,
    load_categories,
    load_series,
    to_hiragana,
)


def test_format_strings_define_the_documented_sizes():
    # The layout is derived from these two strings; if either grows, the size
    # constants move with it instead of a comment going stale.
    assert HEADER_SIZE == 64
    assert TAG_RECORD_SIZE == 24
    assert struct.calcsize(HEADER_FORMAT) == HEADER_SIZE
    assert struct.calcsize(RECORD_FORMAT) == TAG_RECORD_SIZE


def test_encode_varint_boundaries():
    out = bytearray()
    encode_varint(0, out)
    encode_varint(127, out)
    encode_varint(128, out)
    encode_varint(300, out)
    assert bytes(out) == b"\x00\x7f\x80\x01\xac\x02"


def test_to_hiragana_folds_katakana_only():
    assert to_hiragana("ハツネミク") == "はつねみく"
    assert to_hiragana("初音ミク") == "初音みく"
    assert to_hiragana("hatsune") == "hatsune"


def test_complete_month_range_trims_partial_ends_only():
    steady = [1000] * 20
    assert complete_month_range(steady) == (0, 19)
    # A partial sync at the end, and the site's opening week at the start.
    assert complete_month_range(steady[:-1] + [200]) == (0, 18)
    assert complete_month_range([200] + steady[1:]) == (1, 19)
    assert complete_month_range([200] + steady[1:-1] + [150]) == (1, 18)
    # A real dip is kept -- only clearly truncated months are dropped.
    assert complete_month_range(steady[:-1] + [700]) == (0, 19)
    assert complete_month_range([1000, 5]) == (0, 1)             # too short to judge


class BundleReader:
    """Independent reader, deliberately not sharing code with the writer.

    This mirrors what the JavaScript client has to do, so the test doubles as a
    spec check on the on-disk format.
    """

    def __init__(self, buf: bytes):
        (
            magic, self.version, self.n_tags, self.n_months, self.n_categories,
            self.totals_off, self.cat_off, self.table_off, self.names_off,
            self.i18n_off, self.search_off, self.search_len, self.values_off,
            self.epoch_month, self.first_complete, self.last_complete,
        ) = struct.unpack_from(HEADER_FORMAT, buf, 0)
        assert magic == MAGIC
        self.buf = buf
        self.totals = list(struct.unpack_from(f"<{self.n_months}I", buf, self.totals_off))
        self.hay = buf[self.search_off : self.search_off + self.search_len].decode("utf-8")

    @property
    def epoch(self) -> tuple[int, int]:
        return divmod(self.epoch_month, 12)[0], self.epoch_month % 12 + 1

    def category(self, i: int) -> tuple[int, list[int], list[float]]:
        stride = 4 + self.n_months * 8
        o = self.cat_off + i * stride
        (cat,) = struct.unpack_from("<I", self.buf, o)
        posts = list(struct.unpack_from(f"<{self.n_months}I", self.buf, o + 4))
        n_eff = list(struct.unpack_from(f"<{self.n_months}f", self.buf, o + 4 + self.n_months * 4))
        return cat, posts, n_eff

    def _varint(self, p: int) -> tuple[int, int]:
        value = shift = 0
        while True:
            byte = self.buf[p]
            p += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value, p
            shift += 7

    def record(self, i: int) -> dict:
        name_off, name_len, category, deprecated, first_m, span, data_off, post_count, i18n_off = struct.unpack_from(RECORD_FORMAT, self.buf, self.table_off + i * TAG_RECORD_SIZE)
        name = self.buf[self.names_off + name_off : self.names_off + name_off + name_len].decode("utf-8")
        return {"name": name, "category": category, "deprecated": bool(deprecated), "first_m": first_m, "span": span, "data_off": data_off, "post_count": post_count, "i18n_off": i18n_off}

    def names_block(self, i: int) -> list[tuple[str | None, str]]:
        count, p = self._varint(self.i18n_off + self.record(i)["i18n_off"])
        out = []
        for _ in range(count):
            code = self.buf[p]
            length, p = self._varint(p + 1)
            out.append((None if code == ALIAS_CODE else LANGS[code], self.buf[p : p + length].decode("utf-8")))
            p += length
        return out

    def display(self, i: int, lang: str) -> str:
        for code, text in self.names_block(i):
            if code == lang:
                return text
        return self.record(i)["name"]

    def series(self, i: int) -> tuple[int, list[int]]:
        rec = self.record(i)
        p = self.values_off + rec["data_off"]
        out = []
        for _ in range(rec["span"]):
            value, p = self._varint(p)
            out.append(value)
        return rec["first_m"], out

    def row(self, i: int) -> list[str]:
        return self.hay.split("\n")[i].split("\x1f")

    def find(self, name: str) -> int | None:
        lo, hi = 0, self.n_tags - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            found = self.record(mid)["name"]
            if found == name:
                return mid
            if found < name:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    def search(self, query: str) -> list[str]:
        """Mirror of the client: sweep the haystack, map hits back to rows."""
        q = query.lower()
        starts, pos = [], 0
        for line in self.hay.split("\n")[: self.n_tags]:
            starts.append(pos)
            pos += len(line) + 1
        hits, at = [], self.hay.find(q)
        while at >= 0:
            i = max(k for k, s in enumerate(starts) if s <= at)
            if i not in hits:
                hits.append(i)
            at = self.hay.find(q, at + 1)
        return [self.record(i)["name"] for i in hits]


def write_index_parquets(con, tmp_path, monthly_rows, total_rows, dim_rows, category_rows=()):
    """Materialise the Parquet inputs load_series/load_categories expect."""
    def copy(name, columns, rows):
        # Elements are raw SQL literals ("DATE '2005-05-01'", "'alpha'", 10), so
        # they are joined verbatim -- Python's repr would quote them wrongly.
        values = ", ".join("(" + ", ".join(str(v) for v in row) + ")" for row in rows)
        con.execute(f"COPY (SELECT * FROM (VALUES {values}) t({columns})) TO '{(tmp_path / name).as_posix()}' (FORMAT PARQUET)")

    copy("fact_tag_monthly.parquet", "tag_id, month, posts", monthly_rows)
    copy("fact_total_monthly.parquet", "month, posts", total_rows)
    copy("dim_tag.parquet", "tag_id, name, category, post_count, is_deprecated", dim_rows)
    if category_rows:
        copy("fact_category_monthly.parquet", "category, month, posts, n_eff", category_rows)


def build(con, tmp_path, min_post_count=0, name_maps=None, other_names=None, converters=None):
    rows, totals, epoch = load_series(con, tmp_path, min_post_count)
    categories = load_categories(con, tmp_path, f"{epoch}-01", len(totals))
    sections, stats = build_sections(rows, totals, categories, name_maps or {}, other_names or {}, converters)
    year, month = (int(p) for p in epoch.split("-"))
    first, last = complete_month_range(totals)
    buf = assemble(sections, stats["tags"], len(totals), len(categories), year * 12 + (month - 1), first, last)
    return BundleReader(buf), epoch, stats


@pytest.fixture
def bundle(tmp_path):
    con = duckdb.connect()
    # zebra has a gap (months 0 and 2, nothing in 1) and a value needing 3 varint
    # bytes; alpha spans every month; beta starts late.
    write_index_parquets(
        con,
        tmp_path,
        monthly_rows=[
            (1, "DATE '2005-05-01'", 10), (1, "DATE '2005-06-01'", 200), (1, "DATE '2005-07-01'", 3),
            (2, "DATE '2005-07-01'", 7),
            (3, "DATE '2005-05-01'", 1), (3, "DATE '2005-07-01'", 20000),
        ],
        total_rows=[("DATE '2005-05-01'", 100), ("DATE '2005-06-01'", 400), ("DATE '2005-07-01'", 900)],
        dim_rows=[
            (1, "'alpha'", 0, 213, "false"), (2, "'beta'", 4, 7, "true"), (3, "'zebra'", 3, 20001, "false"),
        ],
        category_rows=[
            (3, "DATE '2005-05-01'", 1, 1.0), (3, "DATE '2005-07-01'", 20000, 1.5),
            (4, "DATE '2005-07-01'", 7, 2.0),
        ],
    )
    name_maps = {"beta": {"zh_hans": "贝塔", "ja": "ベータ", "en": "Beta"}, "zebra": {"zh_hant": "斑馬"}}
    other_names = {"alpha": ["アルファ", "第一", "Alpha"], "beta": ["ベータ"]}
    converters = Converters(lambda s: s.replace("斑馬", "斑马"), lambda s: s.replace("贝塔", "貝塔"))
    reader, epoch, stats = build(con, tmp_path, name_maps=name_maps, other_names=other_names, converters=converters)
    con.close()
    return reader, epoch, stats


def test_header_carries_counts_epoch_and_completeness(bundle):
    reader, epoch, stats = bundle
    assert reader.version == VERSION
    assert (reader.n_tags, stats["tags"]) == (3, 3)
    assert reader.n_months == 3
    assert epoch == "2005-05"
    # The client must not need to hardcode any of these.
    assert reader.epoch == (2005, 5)
    assert (reader.first_complete, reader.last_complete) == (0, 2)
    assert reader.totals == [100, 400, 900]


def test_section_offsets_are_explicit_and_ordered(bundle):
    reader, _, _ = bundle
    offsets = [reader.totals_off, reader.cat_off, reader.table_off, reader.names_off, reader.i18n_off, reader.search_off, reader.values_off]
    assert offsets[0] == HEADER_SIZE
    assert offsets == sorted(offsets), "sections must not overlap or reorder"
    assert reader.table_off + reader.n_tags * TAG_RECORD_SIZE == reader.names_off


def test_category_section_roundtrips(bundle):
    reader, _, _ = bundle
    assert reader.n_categories == 2
    copyright_cat, posts, n_eff = reader.category(0)
    assert copyright_cat == 3
    assert posts == [1, 0, 20000]           # densified onto the month axis
    assert n_eff == pytest.approx([1.0, 0.0, 1.5])
    character_cat, posts, _ = reader.category(1)
    assert (character_cat, posts) == (4, [0, 0, 7])


def test_tags_sorted_by_name_for_binary_search(bundle):
    reader, _, _ = bundle
    assert [reader.record(i)["name"] for i in range(reader.n_tags)] == ["alpha", "beta", "zebra"]
    assert reader.find("beta") == 1
    assert reader.find("missing") is None


def test_series_roundtrip_with_gaps(bundle):
    reader, _, _ = bundle
    assert reader.series(reader.find("alpha")) == (0, [10, 200, 3])
    # A gap inside the run is stored as an explicit zero, not skipped.
    assert reader.series(reader.find("zebra")) == (0, [1, 0, 20000])


def test_run_starts_at_first_active_month(bundle):
    reader, _, _ = bundle
    assert reader.series(reader.find("beta")) == (2, [7])
    assert reader.record(reader.find("beta"))["span"] == 1


def test_metadata_survives(bundle):
    reader, _, _ = bundle
    beta = reader.record(reader.find("beta"))
    assert (beta["category"], beta["deprecated"], beta["post_count"]) == (4, True, 7)


# ---- multilingual ----------------------------------------------------------


def test_display_names_are_language_tagged(bundle):
    reader, _, _ = bundle
    beta = reader.find("beta")
    assert reader.display(beta, "zh_hans") == "贝塔"
    assert reader.display(beta, "ja") == "ベータ"
    assert reader.display(beta, "en") == "Beta"
    # No Korean entry, so it falls back to the raw tag name.
    assert reader.display(beta, "ko") == "beta"


def test_wiki_aliases_are_search_only(bundle):
    reader, _, _ = bundle
    block = reader.names_block(reader.find("alpha"))
    assert all(lang is None for lang, _ in block), "alpha has no name_map entry, so nothing is language-tagged"
    # "Alpha" is dropped: it lowercases to the tag name itself, so it adds no
    # searchability, and without a language tag it cannot serve as a display name.
    assert {text for _, text in block} == {"アルファ", "第一"}


def test_wiki_aliases_never_become_display_names(bundle):
    """Regression guard: alias pools are built for recall, not equivalence.

    alpha's only names are wiki aliases (アルファ, 第一). Labelling one of them
    as the Japanese or Chinese name would state a translation the data does not
    support -- upstream this produced "school_uniform is 制服スパッツ".
    """
    reader, _, _ = bundle
    alpha = reader.find("alpha")
    for lang in LANGS:
        assert reader.display(alpha, lang) == "alpha"


def test_han_conversion_fills_the_missing_chinese_script(bundle):
    reader, _, _ = bundle
    assert reader.display(reader.find("zebra"), "zh_hant") == "斑馬"
    assert reader.display(reader.find("zebra"), "zh_hans") == "斑马"
    assert reader.display(reader.find("beta"), "zh_hans") == "贝塔"
    assert reader.display(reader.find("beta"), "zh_hant") == "貝塔"


def test_alias_duplicating_a_display_name_is_dropped(bundle):
    reader, _, _ = bundle
    texts = [text for _, text in reader.names_block(reader.find("beta"))]
    assert texts.count("ベータ") == 1


def test_names_block_count_matches_entries_written(bundle):
    """The count precedes the entries, so no entry may be skipped after it."""
    reader, _, _ = bundle
    for i in range(reader.n_tags):
        block = reader.names_block(i)
        assert all(text for _, text in block), "a desynced count reads garbage into the next tag"


def test_search_finds_tags_in_any_language(bundle):
    reader, _, _ = bundle
    assert "beta" in reader.search("贝塔")
    assert "beta" in reader.search("ベータ")
    assert "alpha" in reader.search("第一")
    assert "zebra" in reader.search("斑馬")


def test_search_folds_katakana_to_hiragana(bundle):
    reader, _, _ = bundle
    assert "alpha" in reader.search("あるふぁ")
    assert "beta" in reader.search("べーた")


def test_search_applies_han_conversion(bundle):
    reader, _, _ = bundle
    assert "zebra" in reader.search("斑马")


def test_search_rows_align_with_tag_order(bundle):
    reader, _, _ = bundle
    for i in range(reader.n_tags):
        assert reader.row(i)[0] == reader.record(i)["name"]


def test_no_i18n_still_produces_searchable_rows(tmp_path):
    con = duckdb.connect()
    write_index_parquets(
        con, tmp_path,
        monthly_rows=[(1, "DATE '2005-05-01'", 10)],
        total_rows=[("DATE '2005-05-01'", 100)],
        dim_rows=[(1, "'solo'", 0, 500, "false")],
    )
    reader, _, stats = build(con, tmp_path)
    con.close()
    assert stats["translated"] == 0
    assert reader.n_categories == 0
    assert reader.names_block(0) == []
    assert reader.search("solo") == ["solo"]


def test_min_post_count_filters_tags(tmp_path):
    con = duckdb.connect()
    write_index_parquets(
        con, tmp_path,
        monthly_rows=[(1, "DATE '2005-05-01'", 10), (2, "DATE '2005-05-01'", 1)],
        total_rows=[("DATE '2005-05-01'", 100)],
        dim_rows=[(1, "'kept'", 0, 500, "false"), (2, "'dropped'", 0, 5, "false")],
    )
    reader, _, stats = build(con, tmp_path, min_post_count=100)
    con.close()
    assert stats["tags"] == 1
    assert reader.record(0)["name"] == "kept"
